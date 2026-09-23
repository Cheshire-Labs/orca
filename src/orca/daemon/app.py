"""FastAPI factory for the orca daemon.

A fresh daemon has no system loaded: `app.state.system_runtime` starts as
None. `POST /mount-topology` creates a SystemRuntime; `POST /workflows`
registers a workflow on it; `POST /unload` tears it down. Routes that
require a loaded system check via `_require_system_runtime` and return 409
when the slot is empty.

`create_app` takes two injection points:
- `initial_system_runtime`: tests pass a pre-started SystemRuntime to skip
  the mount path. Production passes None; `POST /mount-topology` populates it.
- `on_exit`: callback invoked after `POST /shutdown` flushes its response.
  Production passes a SIGTERM self-signal to terminate the daemon process.
  Tests pass None (or a no-op) so the test runner is not killed.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from orca.daemon.event_stream import SseEventSink
from orca.daemon.operations_router import create_operations_router
from orca.daemon.routes import create_router
from orca.gateway.websocket.connection_events import connection_events
from orca.gateway.websocket.manager import connection_manager
from orca.gateway.websocket.health_monitor import WireHealthMonitor
from orca.gateway.websocket.router import router as gateway_router
from orca.runtime.danger import ConfirmationRequired
from orca.runtime.deployment_registries import (
    DeploymentRegistries,
    build_in_memory_deployment_layer,
)
from orca.runtime.system_runtime import SystemRuntime


OnExit = Callable[[], Awaitable[None]]


async def _emit_dropped_disconnects(
    dropped: list[tuple[str, list[str]]],
) -> None:
    """Fire device.disconnected for every device on each pruned client."""
    for _client_id, device_ids in dropped:
        for device_id in device_ids:
            await connection_events.emit_disconnected(device_id)


@asynccontextmanager
async def _daemon_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the stale-connection sweep for the daemon's /ws/devices gateway.

    Without this, missed-heartbeat connections are never pruned and
    device.disconnected never fires for dead device bridges. The monitor stops
    on shutdown.
    """
    monitor = WireHealthMonitor(connection_manager)
    monitor.start(on_dropped=_emit_dropped_disconnects)
    app.state.wire_health_monitor = monitor
    try:
        yield
    finally:
        await monitor.stop()
        # Dispose the daemon-lifetime store engines (access-config + catalog) so
        # their aiosqlite worker threads do not outlive the loop at process exit.
        store_factory = getattr(app.state, "store_factory", None)
        if store_factory is not None:
            await store_factory.aclose()


def create_app(
    initial_system_runtime: SystemRuntime | None = None,
    on_exit: OnExit | None = None,
) -> FastAPI:
    """Build a FastAPI app for a daemon.

    See module docstring for the injection-point contract.
    """
    app = FastAPI(
        title="orca-daemon",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=_daemon_lifespan,
    )
    app.state.system_runtime = initial_system_runtime
    app.state.topology = None
    app.state.spec = None
    app.state.sim = False
    app.state.mounting = None
    app.state.on_exit = on_exit
    # Deployment-scoped registries (labware catalog, access configs, profiles)
    # live for the daemon's lifetime, in front of any mounted system, so the
    # operator CRUD surface works pre-runtime. They share the SAME store
    # instances the runtime resolves against (single source of truth):
    #  - injected runtime (tests): build the layer over the runtime's stores.
    #  - fresh daemon: hold ONE store factory; mount reuses it so the runtime
    #    and the registries layer never diverge.
    if initial_system_runtime is not None:
        app.state.store_factory = None
        app.state.deployment_registries = DeploymentRegistries(
            labware_catalog=initial_system_runtime.labware_catalog_store,
            access_config_store=initial_system_runtime.access_config_store,
            move_defaults_service=initial_system_runtime.move_defaults_service,
            grip_profile_service=initial_system_runtime.grip_profile_service,
            profile_store=initial_system_runtime.profile_store,
        )
    else:
        app.state.store_factory, app.state.deployment_registries = (
            build_in_memory_deployment_layer()
        )

    # One sink for the daemon's lifetime. SSE subscribers register queues
    # on this; the /mount-topology route hands it to each new SystemRuntime.
    # The sink persists across mount/unload cycles so a reconnecting client
    # does not have to reestablish the subscription across every reload.
    app.state.event_sink = SseEventSink()

    # Pre-loaded runtime (used by tests) gets the sink attached now too.
    if initial_system_runtime is not None:
        initial_system_runtime.register_sink(app.state.event_sink)

    # No per-request `current_run_mode` seed: each read states the base it
    # means, and execution entrypoints seed for dispatch.

    @app.exception_handler(ConfirmationRequired)
    async def _confirmation_required_handler(
        _request: Request, exc: ConfirmationRequired,
    ) -> JSONResponse:
        """Map ConfirmationRequired to 400 with the prompt descriptor.

        All daemon mutation routes pass confirm=True today so this is a
        defensive net for future routes / callers that forget. The body
        carries the descriptor so a UI can render the prompt without
        hardcoding copy.
        """
        descriptor = exc.descriptor
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": str(exc),
                "code": "confirmation_required",
                "action_name": exc.action_name,
                "danger_level": descriptor.danger_level.name,
                "message": descriptor.message,
                "requires_reason": descriptor.requires_reason,
                "parameters": [
                    {
                        "name": p.name,
                        "type_name": p.type_name,
                        "required": p.required,
                        "default": p.default,
                        "description": p.description,
                    }
                    for p in descriptor.parameters
                ],
                "call_args": exc.call_args,
            },
        )

    app.include_router(create_router())
    app.include_router(create_operations_router())
    # The device-bridge gateway: a standalone daemon serves /ws/devices so an
    # on-prem device bridge can connect and drive real hardware. Auth defaults
    # to allow-all (single-node LAN); a hosting layer overrides via
    # app.state.connection_authenticator.
    app.include_router(gateway_router)
    return app
