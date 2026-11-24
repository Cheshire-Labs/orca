"""FastAPI factory for the orca daemon.

`create_app` builds every part once through `orca.runtime.startup` and holds it in one `DaemonState`. The
activity database opens when the app starts, and runs the last process left
running are marked failed then. A fresh daemon has no system loaded.
`DaemonState.holder`, the `RuntimeHolder` both servers use, holds the mounted system.
`POST /api/runtime/mount-package` builds the package folder (its `topology.py` and
`workflows/`), `POST /api/runtime/reload` rebuilds it from the files on disk,
and `POST /api/runtime/unload` tears it down. Every route but `/health`, `/shutdown` and the websockets is an
Operation under `/api`; one that needs a loaded system answers 503
`runtime_not_ready` without one.

The daemon serves the device-bridge gateway itself, so the runtime it mounts
reads connections from the tracker `/ws/devices` writes.

`create_app` takes two injection points:
- `injected`: tests pass a started SystemRuntime with the setup and activity
  stores it was built over, to skip the mount path. Production passes None.
- `on_exit`: callback invoked after `POST /shutdown` flushes its response.
  Production passes a SIGTERM self-signal to terminate the daemon process.
  Tests pass None (or a no-op) so the test runner is not killed.
"""

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, FastAPI
from pydantic import TypeAdapter, ValidationError
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from orca.api.event_feed import EventFeed
from orca.runtime.server_events import ServerEvents
from orca.api import health_router, install_error_handlers, mount_api, mount_event_stream
from orca.daemon.api_context import DaemonApiContext, LocalhostAuth
from orca.gateway.connection_handling import device_connection_handling
from orca.daemon.daemon_state import DaemonState, OnExit
from orca.daemon.check_run_stores import SqliteCheckRunStores, check_runs_folder
from orca.daemon.operation_catalog import DaemonOperationCatalog
from orca.gateway.auth import NullConnectionAuthenticator
from orca.gateway.device_gateway import ServerDeviceGateway
from orca.gateway.websocket.router import create_gateway_router
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.system_loading_models import SHUTDOWN_ROUTE
from orca.operations.wire_models import ShutdownResponse
from orca.operations.audit import StoredAuditLog
from orca.operations.events import StoredEventLog
from orca.daemon.activity_database import ActivityDatabase
from orca.runtime.activity_database import IActivityStores, state_db_path
from orca.runtime.interrupted_runs import mark_interrupted_executions
from orca.runtime.runtime_holder import RuntimeHolder
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.sinks import LogSink
from orca.modes.sim_pacing import FRAMEWORK_SIM_SPEEDUP, parse_default_sim_speedup
from orca.plugins.config import PluginConfig
from orca.runtime.startup import (
    ConfiguredPlugins,
    PackageSetupStoreFactory,
    build_device_gateway,
    build_server_parts,
    load_plugins,
    operator_device_commands,
    runtime_settings,
    start_log_store_writers,
)
from orca.runtime.store_factory import IRuntimeStoreFactory, default_package_root


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InjectedSystem:
    """A started runtime a test hands the daemon, with the stores and gateway it was built over."""

    runtime: ISystemRuntime
    setup: IRuntimeStoreFactory
    activity: IActivityStores
    gateway: ServerDeviceGateway


def default_client_from_env() -> str:
    """``ORCA_DEFAULT_CLIENT`` names the orca-client that runs devices whose setup names none; unset means ``local``."""
    return os.environ.get("ORCA_DEFAULT_CLIENT", "").strip() or "local"


def default_sim_speedup_from_env() -> float | None:
    """``ORCA_SIM_SPEEDUP`` is the deployment default: unset is 100, ``off`` means no waiting."""
    try:
        return parse_default_sim_speedup(os.environ.get("ORCA_SIM_SPEEDUP"), FRAMEWORK_SIM_SPEEDUP)
    except ValueError as e:
        raise OperationError.of(
            ErrorCode.VALIDATION_ERROR,
            f"ORCA_SIM_SPEEDUP is not a sim speed-up: {e}",
        ) from e


def plugins_from_env() -> ConfiguredPlugins:
    """``ORCA_PLUGINS`` is a JSON list of plugin configs, such as ``[{"type": "webhook", "url": "..."}]``; unset means none."""
    raw = os.environ.get("ORCA_PLUGINS", "").strip()
    if not raw:
        return ConfiguredPlugins()
    try:
        configs = TypeAdapter(list[PluginConfig]).validate_json(raw)
    except ValidationError as e:
        raise ValueError(f"ORCA_PLUGINS is not a JSON list of plugin configs: {e}") from e
    return load_plugins(configs)


async def _mark_interrupted_runs(daemon: DaemonState) -> None:
    # A bad run record must not stop the daemon; it is logged and startup continues.
    try:
        await mark_interrupted_executions(daemon.activity.execution_records, [daemon.events])
    except Exception:
        _logger.exception("could not mark the runs the last daemon left running")


async def _unload_on_exit(holder: RuntimeHolder) -> None:
    # The rest of the teardown must run even when the unload fails.
    if not holder.is_built:
        return
    try:
        await holder.unload()
    except Exception:
        _logger.exception("the mounted system did not unload as the daemon stopped")


def _daemon_lifespan(daemon: DaemonState) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Open the activity database and run the /ws/devices gateway for the life of the app.

    Runs the last process left running are marked failed before anything mounts.
    Every audit row and event is written to the activity database while the app runs.
    The device connection handling every gateway host runs is wired for the life of the app.
    However the app stops, a mounted system is unloaded first, as `POST /shutdown` does.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await daemon.activity.open()
        writers = start_log_store_writers(
            daemon.activity.audit, daemon.activity.events, daemon.activity.unsaved_records, daemon.activity.event_saves,
        )
        daemon.plugins.start(daemon.events)
        await _mark_interrupted_runs(daemon)
        try:
            async with device_connection_handling(daemon.gateway, daemon.events) as monitor:
                app.state.wire_health_monitor = monitor
                try:
                    yield
                finally:
                    await _unload_on_exit(daemon.holder)
        finally:
            await daemon.plugins.stop()
            for writer in writers:
                await writer.stop()
            await daemon.activity.aclose()

    return lifespan


def _daemon_state(
    injected: InjectedSystem | None, on_exit: OnExit | None, package_root: Path,
) -> DaemonState:
    # One feed for the daemon's lifetime, so a /ws/events subscriber keeps
    # receiving across mount and unload.
    event_sink = EventFeed()
    events = ServerEvents()
    events.add_sink(LogSink())
    events.add_sink(event_sink)
    # Every runtime this daemon mounts reads connections from the tracker that /ws/devices writes.
    gateway = injected.gateway if injected is not None else build_device_gateway(events, default_sim_speedup_from_env())

    def _mounted() -> ISystemRuntime | None:
        return daemon.holder.built_runtime

    setup: IRuntimeStoreFactory
    activity: IActivityStores
    if injected is not None:
        injected.runtime.register_sink(events)
        setup, activity = injected.setup, injected.activity
    else:
        # Setup lives in the package folder's files, so the operator
        # surface works before a mount and the runtime reads the same files after.
        setup = PackageSetupStoreFactory(package_root, default_client=default_client_from_env())
        activity = ActivityDatabase(state_db_path(package_root))

    # The device factory is bound while the package is built, so every device it declares is driven through the device bridge.
    parts = build_server_parts(setup, gateway, _mounted)
    plugins = plugins_from_env()
    holder = RuntimeHolder(
        package_dir=package_root,
        setup=setup,
        activity=activity,
        connections=parts.connections,
        sinks=[events],
        labware_events=events,
        device_factory=parts.device_factory,
        plugins=plugins,
        settings=runtime_settings(default_sim_speedup_from_env()),
        check_run_stores=SqliteCheckRunStores(check_runs_folder(package_root)),
        runtime=injected.runtime if injected is not None else None,
    )
    daemon = DaemonState(
        package_root=package_root,
        setup=setup,
        activity=activity,
        registries=parts.registries,
        audit_log=StoredAuditLog(activity.audit),
        event_log=StoredEventLog(activity.events),
        connections=parts.connections,
        gateway=gateway,
        event_sink=event_sink,
        events=events,
        device_commands=operator_device_commands(gateway, _mounted),
        plugins=plugins,
        on_exit=on_exit,
        holder=holder,
    )
    return daemon


def _shutdown(daemon: DaemonState) -> APIRouter:
    router = APIRouter()

    @router.post(
        SHUTDOWN_ROUTE.binding.path, response_model=SHUTDOWN_ROUTE.response, tags=[SHUTDOWN_ROUTE.binding.tag],
    )
    async def shutdown() -> JSONResponse:
        """Unload if needed, then exit the process once this response is sent."""
        if daemon.holder.is_built:
            await daemon.holder.unload()
        # The exit runs after the response flushes, or uvicorn would tear down mid-write.
        background = BackgroundTask(daemon.on_exit) if daemon.on_exit is not None else None
        return JSONResponse(
            content=ShutdownResponse().model_dump(mode="json"), background=background,
        )

    return router


def create_app(
    on_exit: OnExit | None = None,
    package_root: Path | None = None,
    injected: InjectedSystem | None = None,
) -> FastAPI:
    """Build a FastAPI app for a daemon.

    See module docstring for the injection-point contract.
    """
    daemon = _daemon_state(injected, on_exit, package_root or default_package_root())
    app = FastAPI(
        title="orca-daemon",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=_daemon_lifespan(daemon),
    )
    app.state.daemon = daemon

    # No per-request `current_run_mode` seed: each read states the base it
    # means, and execution entrypoints seed for dispatch.
    install_error_handlers(app)

    app.include_router(health_router(lambda: daemon.holder))
    app.include_router(_shutdown(daemon))
    operations_router = APIRouter()
    mount_api(
        operations_router, DaemonOperationCatalog(),
        lambda _request: DaemonApiContext(daemon), LocalhostAuth(),
    )
    app.include_router(operations_router)
    mount_event_stream(app.router, lambda _websocket: daemon.event_sink, LocalhostAuth())
    # A standalone daemon serves /ws/devices so an on-prem device bridge can
    # connect. It trusts its LAN, so the handshake allows every caller.
    app.include_router(create_gateway_router(
        daemon.gateway,
        authenticator=NullConnectionAuthenticator(),
    ))
    return app
