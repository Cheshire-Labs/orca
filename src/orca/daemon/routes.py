"""FastAPI route handlers wrapping the daemon's (optional) SystemRuntime.

The daemon may be running without a loaded system (the state right after
`orca start` before `orca topology mount`). Routes that operate on a system
check via `_require_system_runtime` and return 409 when the slot is empty.

Mount/load/unload (the split ingress):
- POST /mount-topology builds an empty SystemRuntime from a build_topology
  factory spec and starts it.
- POST /workflows registers a workflow (build_workflow factory spec) against
  the mounted topology; no execution starts.
- POST /unload shuts the SystemRuntime down and clears the slot; daemon keeps running.
- POST /shutdown unloads first (if needed) and terminates the daemon.

Error mapping (runtime -> HTTP):
- KeyError           -> 404 (unknown execution / thread id)
- RuntimeError       -> 409 (runtime not RUNNING state; already mounted; not loaded)
- ValueError         -> 409 (illegal state transition)
- system_builder exceptions -> 400/404 per typed class
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import JsonValue, ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.teachpoints import AccessConfig

from orca.daemon import system_builder
from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    CommandTimeoutError,
    ConfirmationRequiredError,
    DeviceError,
    DeviceLockedError,
    DeviceOfflineError,
    DeviceUnknownError,
    InvalidCommandError,
    ModeUnresolvableError,
)
from orca.daemon.bindings import bind_orca_rest
from orca.daemon.route_tags import RouteTag
from orca.operations._protocol import OperationError, message_of
from orca.operations.submission import (
    GetSubmissionOperation,
    ListSubmissionsOperation,
    SubmitExecutionOperation,
)
from orca.operations.submission_models import (
    GetSubmissionRequest,
    GetSubmissionResponse,
    ListSubmissionsRequest,
    ListSubmissionsResponse,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)
from orca.variables.deployment_profile import DeploymentProfile
from orca.variables.errors import UndefinedVariableError
from orca.gateway.adhoc import fault_summary
from orca.daemon.schemas import (
    AccessConfigDTO,
    GripProfileDTO,
    GripProfilePatchRequest,
    MoveDefaultsDTO,
    MoveDefaultsPatchRequest,
    AuditEntryDTO,
    AuditListResponse,
    CommandDescriptorDTO,
    CreateTeachpointRequest,
    DeckLayoutDTO,
    DeckLayoutSummaryDTO,
    TeachpointDTO,
    UpdateTeachpointRequest,
    DeviceDTO,
    DeviceFaultDTO,
    DeviceExecuteRequest,
    DeviceConnectResponse,
    DeviceDisconnectResponse,
    DeviceLifecycleRequest,
    DeviceInitializeResponse,
    DeviceIntrospectionDTO,
    DeviceInvocationResultDTO,
    DeviceInvokeRequest,
    DeviceModeEligibilityDTO,
    DeviceRegistryEntryDTO,
    DeviceRegistryListDTO,
    IncidentAckResponse,
    RecoverableTimeoutDecisionResponse,
    RecoverableTimeoutExtendRequest,
    RecoverableTimeoutOperatorRequest,
    IncidentDTO,
    LabwareCatalogCreateRequest,
    LabwareCatalogListResponse,
    LabwareCatalogUpdateRequest,
    ParamSpecDTO,
    PluginCommandDTO,
    PluginDTO,
    ReservationCancelResponse,
    ExecutionRecordDTO,
    HealthResponse,
    MountTopologyRequest,
    MountTopologyResponse,
    RegisterWorkflowRequest,
    RegisterWorkflowResponse,
    LabwareClearResponse,
    LabwareClearSubmissionResponse,
    LocationDTO,
    MethodTemplateDTO,
    CrossExecVariableDTO,
    ReservationSnapshotDTO,
    RuntimeStatusResponse,
    ShutdownResponse,
    TopologySourceResponse,
    TopologyViewResponse,
    MoverDTO,
    TransporterDTO,
    ResourcePoolDTO,
    LabwareTemplateDTO,
    SubmitMethodExecutionRequest,
    SubmitWorkflowRequest,
    SystemInfoDTO,
    ThreadSnapshotDTO,
    ThreadTemplateDTO,
    UnloadResponse,
    VariableSetRequest,
    VariableResolutionResponse,
    VariableSetResponse,
    VariableUnsetResponse,
    VariableValue,
    VariablesListResponse,
    WorkflowTemplateDTO,
)
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.sim_diagnostics import enable_sim_coroutine_diagnostics
from orca.runtime.runtime_interface import (
    ConcurrentLiveSimRefusedError,
    LiveSubmissionWithSimOverridesUnacknowledgedError,
    RunModeMismatchError,
    RunModeRequiredError,
    StartLocationsOccupiedError,
)
from orca.runtime.danger import list_audit_entries
from orca.runtime.labware_catalog_protocol import LabwareNotFound
from orca.runtime.labware_catalog_store import (
    OPERATOR_CUSTOM_SOURCE,
    LabwareCatalogEntry,
)
from orca.runtime.labware_catalog_service import (
    LabwareCatalogConflict,
    LabwareGeometryInvalid,
    SeedLabwareReadOnly,
)
from orca.runtime.access_config_store import (
    AccessConfigInUseError,
    ProtectedAccessConfigError,
)
from orca.runtime.teachpoint_wire import (
    InvalidTeachpointCoordsError,
    build_teachpoint,
    coord_type_for,
    validate_coords,
)
from orca.runtime.deployment_registries import (
    build_in_memory_deployment_layer,
    IDeploymentRegistries,
)
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import build_system


logger = logging.getLogger(__name__)


_NO_SYSTEM_LOADED_DETAIL = "no system loaded; POST /mount-topology first"


def _raise_run_mode_required(exc: RunModeRequiredError) -> NoReturn:
    """Translate RunModeRequiredError into a typed 422 envelope.

    Sim-hierarchy v3.4 surface contract: `detail` carries `code`,
    `message`, and an `extras` object so downstream clients (CLI,
    a hosted deployment) can parse the typed fields rather than scraping str(e).
    """
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "RUN_MODE_REQUIRED",
            "message": str(exc),
            "extras": {},
        },
    )


def _raise_run_mode_mismatch(
    exc: RunModeMismatchError,
) -> NoReturn:
    """Translate RunModeMismatchError into a typed 409 envelope.

    Fires when a JOIN_EXISTING submission's run_mode differs from the
    existing execution's run_mode. Replaces the v3.4 refuse-all
    `CONCURRENT_SUBMISSION_REFUSED` envelope. STANDALONE submissions
    never hit this path; each gets its own fresh execution.
    """
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "RUN_MODE_MISMATCH",
            "message": str(exc),
            "extras": {
                "blocking_execution_id": exc.blocking_execution_id,
                "blocking_workflow_name": exc.blocking_workflow_name,
                "existing_run_mode": exc.existing_run_mode.value,
                "submitted_run_mode": exc.submitted_run_mode.value,
            },
        },
    )


def _raise_concurrent_live_sim_refused(
    exc: ConcurrentLiveSimRefusedError,
) -> NoReturn:
    """Translate ConcurrentLiveSimRefusedError into a typed 409 envelope.

    Fires when a new execution's live/sim world conflicts with an
    already-running execution. Live and sim cannot run concurrently
    because they target the same devices.
    """
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "CONCURRENT_LIVE_SIM_REFUSED",
            "message": str(exc),
            "extras": {
                "blocking_execution_id": exc.blocking_execution_id,
                "blocking_workflow_name": exc.blocking_workflow_name,
                "existing_run_mode": exc.existing_run_mode.value,
                "submitted_run_mode": exc.submitted_run_mode.value,
            },
        },
    )


def _raise_start_locations_occupied(
    exc: StartLocationsOccupiedError,
) -> NoReturn:
    """Translate StartLocationsOccupiedError into a typed 409 envelope.

    Fires when a submission would route fresh labware to a location that
    already holds some. Each occupied slot rides in `extras` so the caller
    can name what to clear instead of asking the operator to hunt for it.
    """
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "start_location_occupied",
            "message": str(exc),
            "extras": {
                "occupied": [
                    {
                        "position_id": slot.position_id,
                        "existing_labware_name": slot.existing_labware_name,
                        "existing_template_name": slot.existing_template_name,
                        "source": slot.source,
                    }
                    for slot in exc.occupied
                ],
            },
        },
    )


def _raise_live_sim_overrides_unacknowledged(
    exc: LiveSubmissionWithSimOverridesUnacknowledgedError,
) -> NoReturn:
    """Translate LiveSubmissionWithSimOverridesUnacknowledgedError into a typed
    422 envelope, including the device-override list."""
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED",
            "message": str(exc),
            "extras": {
                "devices": [
                    {
                        "name": name,
                        "sim_override": override.value,
                        "resolved_mode": resolved.value,
                    }
                    for name, override, resolved in exc.devices
                ],
            },
        },
    )


# Gateway failures an operator can act on. Both device dispatch routes send
# over the wire now, so these reach the route rather than the 400/404 pair the
# facade used to raise on its own.
_GATEWAY_STATUS: dict[type[BaseException], int] = {
    DeviceUnknownError: status.HTTP_404_NOT_FOUND,
    DeviceOfflineError: status.HTTP_503_SERVICE_UNAVAILABLE,
    DeviceLockedError: status.HTTP_409_CONFLICT,
    InvalidCommandError: status.HTTP_400_BAD_REQUEST,
    CommandTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
    CommandExecutionError: status.HTTP_502_BAD_GATEWAY,
    ConfirmationRequiredError: status.HTTP_409_CONFLICT,
    ModeUnresolvableError: status.HTTP_409_CONFLICT,
}


def _gateway_http_error(exc: DeviceError) -> HTTPException:
    # Walked, not looked up: a subclass of a mapped error means the same thing
    # to an operator, and an exact-type lookup would answer 502 for it.
    for cls in type(exc).__mro__:
        code = _GATEWAY_STATUS.get(cls)
        if code is not None:
            return HTTPException(status_code=code, detail=message_of(exc))
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail=message_of(exc),
    )


def _require_system_runtime(request: Request) -> SystemRuntime:
    """Fetch the SystemRuntime or 409 if nothing is loaded."""
    rt = request.app.state.system_runtime
    if rt is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_NO_SYSTEM_LOADED_DETAIL,
        )
    return rt


async def _shut_down_after_a_failed_start(rt: SystemRuntime, spec: str) -> None:
    """Shut down a runtime whose start raised, as /unload would.

    Start can fail after subscribing to device connects and opening stores,
    and nothing else holds the runtime to clean it up.
    """
    try:
        await rt.shutdown(confirm=True)
    except Exception:
        # The start error is the one the operator needs; do not replace it.
        logger.exception("topology %r did not shut down after failing to start", spec)


def _refuse_while_mounting(request: Request) -> None:
    """Refuse a mount or unload that would overlap a mount still in progress.

    Two overlapping mounts each start a runtime, and only one is kept.
    """
    in_progress = request.app.state.mounting
    if in_progress is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a mount of {in_progress!r} is still in progress; wait for it "
                "to finish, then check `orca status`"
            ),
        )


async def _build_and_start(
    request: Request, body: MountTopologyRequest,
) -> MountTopologyResponse:
    """The mount itself: import the topology, build the system, start the runtime."""
    # One store factory for the daemon's life: the runtime and the registries share
    # its stores, and operator edits outlive an unload.
    stores = request.app.state.store_factory
    if stores is None:
        # Only after an injected runtime, which brings no factory, is unloaded.
        stores, request.app.state.deployment_registries = (
            build_in_memory_deployment_layer()
        )
        request.app.state.store_factory = stores
    try:
        topology = system_builder.load_topology_spec(body.spec, stores)
    except system_builder.SpecFormatError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except system_builder.ModuleImportError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except system_builder.FactoryNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except system_builder.FactoryNotCallableError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except system_builder.FactoryReturnShapeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    try:
        build = await build_system(
            name=body.spec,
            topology=topology,
            stores=stores,
            workflow=None,
        )
    except Exception as e:
        # The builder names the offending teachpoint and lists what was
        # registered; letting it escape turns that into an empty 500.
        logger.exception("topology %r failed to build", body.spec)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{body.spec} failed to build: {type(e).__name__}: {e}",
        )
    # Reconcile access-config seeds into the SQLite store; the async Service
    # write cannot run at the sync stores.access_configs(seed=...) call.
    await stores.apply_seeds()
    # `body.sim` only feeds /health; each submission picks its own run mode.
    rt = SystemRuntime(
        build.system,
        event_bus=build.event_bus,
        access_config_service=stores.access_configs(),
        move_defaults_service=stores.move_defaults(),
        grip_profile_service=stores.grip_profiles(),
        profile_store=stores.profiles(),
        labware_catalog_store=stores.labware_catalog_store(),
    )
    if body.sim:
        # Report un-awaited coroutines for the whole sim run; a hardware mount leaves logging alone.
        enable_sim_coroutine_diagnostics()
    try:
        await rt.start()
    except Exception as e:
        # Start validates the built system, so a bad topology fails here
        # rather than in build_system. Same reason as above.
        logger.exception("topology %r failed to start", body.spec)
        await _shut_down_after_a_failed_start(rt, body.spec)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{body.spec} failed to start: {type(e).__name__}: {e}",
        )
    # Wire the daemon's persistent SSE sink so live /events/stream
    # subscribers receive events from the newly mounted topology.
    event_sink = getattr(request.app.state, "event_sink", None)
    if event_sink is not None:
        rt.register_sink(event_sink)
    request.app.state.system_runtime = rt
    request.app.state.topology = topology
    request.app.state.spec = body.spec
    request.app.state.sim = body.sim
    return MountTopologyResponse(
        spec=body.spec, sim=body.sim, runtime_state=rt.state,
    )


def get_deployment_registries(request: Request) -> IDeploymentRegistries:
    """Fetch the always-available deployment registries layer.

    Deployment config (catalog / access configs / profiles) is reachable with
    no system mounted, so these routes never 409 on an empty daemon.
    """
    return request.app.state.deployment_registries


def create_router() -> APIRouter:
    router = APIRouter()

    # -- Lifecycle ---------------------------------------------------------

    @router.get("/health", response_model=HealthResponse, tags=[RouteTag.LIFECYCLE])
    async def health(request: Request) -> HealthResponse:
        rt: SystemRuntime | None = request.app.state.system_runtime
        return HealthResponse(
            system_loaded=rt is not None,
            runtime_state=rt.state if rt is not None else None,
            spec=request.app.state.spec,
            sim=request.app.state.sim,
            mounting=request.app.state.mounting,
        )

    @router.post("/shutdown", response_model=ShutdownResponse, tags=[RouteTag.LIFECYCLE])
    async def shutdown(request: Request) -> JSONResponse:
        """Unload (if needed) and schedule daemon-process exit.

        The process-exit has to run AFTER this response is sent, or uvicorn
        would be torn down while the connection is still being written.
        Starlette's BackgroundTask runs after response flush.

        `app.state.on_exit` is the pluggable termination hook: production
        sets SIGTERM; tests set None (or a no-op) to keep pytest alive.
        """
        rt: SystemRuntime | None = request.app.state.system_runtime
        if rt is not None:
            await rt.shutdown(confirm=True)
            request.app.state.system_runtime = None
            request.app.state.topology = None
            request.app.state.spec = None

        on_exit = request.app.state.on_exit
        background = BackgroundTask(on_exit) if on_exit is not None else None
        # mode="json" serializes the OperationResult enum to its .value string.
        return JSONResponse(
            content=ShutdownResponse().model_dump(mode="json"),
            background=background,
        )

    @router.post(
        "/mount-topology",
        response_model=MountTopologyResponse,
        tags=[RouteTag.LIFECYCLE],
    )
    async def mount_topology(
        request: Request, body: MountTopologyRequest,
    ) -> MountTopologyResponse:
        """Build and start an empty SystemRuntime from a topology factory.

        Workflows register separately via POST /workflows. The topology is
        held on app.state so workflow registration can resolve device refs.
        """
        if request.app.state.system_runtime is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"a topology is already mounted (spec={request.app.state.spec!r});"
                    " POST /unload first"
                ),
            )
        _refuse_while_mounting(request)
        request.app.state.mounting = body.spec
        try:
            return await _build_and_start(request, body)
        finally:
            request.app.state.mounting = None

    @router.post(
        "/workflows",
        response_model=RegisterWorkflowResponse,
        tags=[RouteTag.LIFECYCLE],
    )
    async def register_workflow(
        request: Request, body: RegisterWorkflowRequest,
    ) -> RegisterWorkflowResponse:
        """Register a workflow against the mounted topology. No execution starts."""
        rt = _require_system_runtime(request)
        topology = request.app.state.topology
        if topology is None:
            # Runtime present but no mounted topology: the injected-runtime
            # path (tests / future embedders) skips mount-topology, so there
            # is no Topology to resolve device references against.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="no mounted topology; POST /mount-topology first",
            )
        try:
            template = system_builder.load_workflow_spec(body.spec, topology)
        except system_builder.SpecFormatError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except system_builder.ModuleImportError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        except system_builder.FactoryNotFoundError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        except system_builder.FactoryNotCallableError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except system_builder.FactoryReturnShapeError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        try:
            await rt.registry.add_workflow_template(template, confirm=True)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message_of(e))
        return RegisterWorkflowResponse(workflow_name=template.name)

    @router.post("/unload", response_model=UnloadResponse, tags=[RouteTag.LIFECYCLE])
    async def unload(request: Request) -> UnloadResponse:
        _refuse_while_mounting(request)
        rt: SystemRuntime | None = request.app.state.system_runtime
        if rt is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="nothing to unload; no system is loaded",
            )
        await rt.shutdown(confirm=True)
        request.app.state.system_runtime = None
        request.app.state.topology = None
        request.app.state.spec = None
        return UnloadResponse()

    # -- Executions --------------------------------------------------------

    @router.post("/executions", response_model=ExecutionRecordDTO, tags=[RouteTag.EXECUTIONS])
    async def submit_workflow(
        request: Request, body: SubmitWorkflowRequest,
    ) -> ExecutionRecordDTO:
        rt = _require_system_runtime(request)

        # Pre-validate the profile before we allocate an execution id. Any
        # file-system or schema error here returns 400 with zero side effects.
        # The second load_profile call below re-reads the file so the
        # @dangerous audit trail records the actual profile path; in practice
        # pre-validation makes the second read effectively infallible.
        if body.profile_path is not None:
            if not os.path.isfile(body.profile_path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"profile_path {body.profile_path!r} does not exist or is not a file",
                )
            try:
                data = json.loads(
                    Path(body.profile_path).read_text(encoding="utf-8"),
                )
                DeploymentProfile.model_validate(data)
            except (OSError, json.JSONDecodeError, ValidationError) as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"profile validation failed: {e}",
                )

        try:
            rec = await rt.submit_workflow(
                body.workflow_name, body.variables,
                mode=WorkflowRunMode[body.run_mode],
                acknowledge_warnings=body.acknowledge_warnings,
            )
        except RunModeRequiredError as e:
            _raise_run_mode_required(e)
        except LiveSubmissionWithSimOverridesUnacknowledgedError as e:
            _raise_live_sim_overrides_unacknowledged(e)
        except RunModeMismatchError as e:
            _raise_run_mode_mismatch(e)
        except ConcurrentLiveSimRefusedError as e:
            _raise_concurrent_live_sim_refused(e)
        except StartLocationsOccupiedError as e:
            _raise_start_locations_occupied(e)
        except RuntimeError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))

        if body.profile_path is not None:
            try:
                rt.variables.load_profile(
                    rec.id, body.profile_path,
                    confirm=True,
                    reason=f"orca run --profile {body.profile_path} at submit time",
                )
            except (OSError, json.JSONDecodeError, ValidationError, KeyError) as e:
                # TOCTOU race: profile was valid at pre-check but failed on
                # apply (file changed, partition missing, etc.). Best-effort
                # cleanup so the execution doesn't leak in a bad state.
                try:
                    await rt.abort_execution(rec.id)
                except (KeyError, RuntimeError):
                    pass
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"profile load failed: {e}",
                )
        return ExecutionRecordDTO.from_dc(rec)

    @router.post("/method-executions", response_model=ExecutionRecordDTO, tags=[RouteTag.EXECUTIONS])
    async def submit_method_execution(
        request: Request, body: SubmitMethodExecutionRequest,
    ) -> ExecutionRecordDTO:
        """Standalone method execution.

        Resolves (workflow_name, method_name) -> method template via the
        named workflow's bundle, builds a synthetic one-method workflow,
        and submits through the regular execution machinery.
        """
        rt = _require_system_runtime(request)
        try:
            rec = await rt.submit_method(
                workflow_name=body.workflow_name,
                method_name=body.method_name,
                labware_start=body.labware_start,
                labware_end=body.labware_end,
                variables=body.variables,
                mode=WorkflowRunMode[body.run_mode],
                acknowledge_warnings=body.acknowledge_warnings,
            )
        except RunModeRequiredError as e:
            _raise_run_mode_required(e)
        except LiveSubmissionWithSimOverridesUnacknowledgedError as e:
            _raise_live_sim_overrides_unacknowledged(e)
        except RunModeMismatchError as e:
            _raise_run_mode_mismatch(e)
        except ConcurrentLiveSimRefusedError as e:
            _raise_concurrent_live_sim_refused(e)
        except StartLocationsOccupiedError as e:
            _raise_start_locations_occupied(e)
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(e),
            )
        return ExecutionRecordDTO.from_dc(rec)

    # Execution reads and lifecycle live on the Operations surface: POST
    # /operations/{list-executions, get-execution, get-execution-detail,
    # stop-execution, remove-execution}. See orca.daemon.operations_router.

    # -- Execution-scoped thread control ----------------------------------

    @router.get(
        "/executions/{execution_id}/threads",
        response_model=list[ThreadSnapshotDTO],
        tags=[RouteTag.THREADS],
    )
    async def list_threads(
        request: Request, execution_id: str,
    ) -> list[ThreadSnapshotDTO]:
        rt = _require_system_runtime(request)
        try:
            snaps = rt.threads.list(execution_id)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return [ThreadSnapshotDTO.from_dc(s) for s in snaps]

    @router.get(
        "/executions/{execution_id}/threads/{thread_id}",
        response_model=ThreadSnapshotDTO,
        tags=[RouteTag.THREADS],
    )
    async def get_thread_detail(
        request: Request, execution_id: str, thread_id: str,
    ) -> ThreadSnapshotDTO:
        rt = _require_system_runtime(request)
        try:
            snap = rt.threads.get(execution_id, thread_id)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return ThreadSnapshotDTO.from_dc(snap)

    @router.get(
        "/executions/{execution_id}/reservations",
        response_model=list[ReservationSnapshotDTO],
        tags=[RouteTag.RESERVATIONS],
    )
    async def list_reservations(
        request: Request, execution_id: str,
    ) -> list[ReservationSnapshotDTO]:
        rt = _require_system_runtime(request)
        try:
            snaps = rt.registry.list_reservations(execution_id)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return [ReservationSnapshotDTO.from_dc(s) for s in snaps]

    @router.get(
        "/reservations",
        response_model=list[ReservationSnapshotDTO],
        tags=[RouteTag.RESERVATIONS],
    )
    async def list_reservations_all(
        request: Request,
    ) -> list[ReservationSnapshotDTO]:
        """Cross-execution reservation enumeration (the ``--all`` form).

        Walks every tracked execution and yields its active reservations
        with the owning ``execution_id`` populated on each row so callers
        can re-scope client-side.
        """
        rt = _require_system_runtime(request)
        out: list[ReservationSnapshotDTO] = []
        for execution in rt.iter_executions():
            for s in rt.registry.list_reservations(execution.id):
                dto = ReservationSnapshotDTO.from_dc(s)
                out.append(dto.model_copy(update={"execution_id": execution.id}))
        return out

    # -- Audit ------------------------------------------------------------

    @router.get("/audit", response_model=AuditListResponse, tags=[RouteTag.AUDIT])
    async def audit_list(
        request: Request,
        action_name: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> AuditListResponse:
        """Return the in-memory @dangerous audit ring buffer.

        For the durable audit trail, read log_dir/orca_audit.log directly
        (rotating file handler configured by configure_logging). The
        in-memory buffer caps at 1000 entries and is wiped on restart;
        this endpoint is for live operator inspection without parsing the
        log file.
        """
        del request  # daemon does not auth; trust the loopback bind
        entries = list_audit_entries()
        if action_name is not None:
            entries = [e for e in entries if e.action_name == action_name]
        # most-recent-last; cap to `limit`.
        entries = entries[-limit:]
        return AuditListResponse(entries=[
            AuditEntryDTO(
                timestamp=e.timestamp,
                action_name=e.action_name,
                danger_level=e.danger_level.name,
                reason=e.reason,
                call_args=e.call_args,
            )
            for e in entries
        ])

    # -- Variables --------------------------------------------------------

    # GET /variables (no execution_id) is registered BEFORE
    # /variables/{execution_id} so FastAPI does not treat the empty
    # suffix as a literal execution_id.
    @router.get("/variables", response_model=list[CrossExecVariableDTO], tags=[RouteTag.VARIABLES])
    async def variables_list_all(
        request: Request,
    ) -> list[CrossExecVariableDTO]:
        """Cross-execution variable enumeration (the ``--all`` form)."""
        rt = _require_system_runtime(request)
        out: list[CrossExecVariableDTO] = []
        for execution in rt.iter_executions():
            for name, value in rt.variables.get_all(execution.id).items():
                out.append(CrossExecVariableDTO(
                    execution_id=execution.id, name=name, value=value,
                ))
        return out

    @router.get(
        "/variables/{execution_id}",
        response_model=VariablesListResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_list(
        request: Request, execution_id: str,
    ) -> VariablesListResponse:
        rt = _require_system_runtime(request)
        try:
            return VariablesListResponse(rt.variables.get_all(execution_id))
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))

    # The resolution route is registered BEFORE /variables/{eid}/{name} only
    # for readability; the segment counts differ so FastAPI cannot confuse them.
    @router.get(
        "/variables/{execution_id}/{name}/resolution",
        response_model=VariableResolutionResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_resolution(
        request: Request, execution_id: str, name: str,
    ) -> VariableResolutionResponse:
        """What every submission of this execution resolves for ``name``."""
        rt = _require_system_runtime(request)
        try:
            resolution = rt.variables.explain(name, execution_id)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return VariableResolutionResponse.from_resolution(resolution)

    @router.get(
        "/variables/{execution_id}/{name}",
        response_model=VariableValue,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_get(
        request: Request, execution_id: str, name: str,
        submission_id: str | None = Query(
            None,
            description="Resolve as this submission's threads do. Omit for the "
                        "value submissions without an override resolve.",
        ),
    ) -> VariableValue:
        rt = _require_system_runtime(request)
        try:
            resolution = rt.variables.explain(name, execution_id)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        if submission_id is not None:
            binding = resolution.for_submission(submission_id)
            if binding is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=str(UndefinedVariableError(name)),
                )
            return VariableValue(
                name=name, execution_id=execution_id,
                value=binding.value, source=binding.source,
            )
        if resolution.value is None or resolution.source is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(UndefinedVariableError(name)),
            )
        return VariableValue(
            name=name, execution_id=execution_id, value=resolution.value,
            source=resolution.source,
            shadowed_by=[o.submission_id for o in resolution.overrides],
        )

    # PUT /variables/global/{name} is registered BEFORE /variables/{eid}/{name}
    # because FastAPI matches in registration order and would otherwise treat
    # "global" as the execution_id.
    @router.put(
        "/variables/global/{name}",
        response_model=VariableSetResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_set_global(
        request: Request, name: str, body: VariableSetRequest,
    ) -> VariableSetResponse:
        rt = _require_system_runtime(request)
        rt.variables.set_global(name, body.value, confirm=True)
        return VariableSetResponse(name=name, value=body.value)

    # Submission-scope writes sit under a static "/submissions" prefix so the
    # execution id can never be read as the literal.
    @router.put(
        "/variables/submissions/{execution_id}/{submission_id}/{name}",
        response_model=VariableSetResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_set_submission(
        request: Request, execution_id: str, submission_id: str, name: str,
        body: VariableSetRequest,
    ) -> VariableSetResponse:
        """Write the layer that outranks the per-execution partition."""
        rt = _require_system_runtime(request)
        try:
            rt.variables.set_submission(
                name, body.value, execution_id, submission_id, confirm=True,
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return VariableSetResponse(name=name, value=body.value)

    @router.delete(
        "/variables/submissions/{execution_id}/{submission_id}/{name}",
        response_model=VariableUnsetResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_unset_submission(
        request: Request, execution_id: str, submission_id: str, name: str,
    ) -> VariableUnsetResponse:
        """Drop one submission's override so it falls through to the layers below."""
        rt = _require_system_runtime(request)
        try:
            existed = rt.variables.has_submission_value(
                name, execution_id, submission_id,
            )
            rt.variables.unset_submission(
                name, execution_id, submission_id, confirm=True,
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return VariableUnsetResponse(existed=existed)

    @router.delete(
        "/variables/global/{name}",
        response_model=VariableUnsetResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_unset_global(
        request: Request, name: str,
    ) -> VariableUnsetResponse:
        rt = _require_system_runtime(request)
        rt.variables.unset_global(name, confirm=True)
        return VariableUnsetResponse()

    @router.put(
        "/variables/{execution_id}/{name}",
        response_model=VariableSetResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_set(
        request: Request, execution_id: str, name: str,
        body: VariableSetRequest,
    ) -> VariableSetResponse:
        rt = _require_system_runtime(request)
        try:
            rt.variables.set(name, body.value, execution_id, confirm=True)
            resolution = rt.variables.explain(name, execution_id)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return VariableSetResponse(
            name=name, value=body.value,
            shadowed_by=[o.submission_id for o in resolution.overrides],
        )

    @router.delete(
        "/variables/{execution_id}/{name}",
        response_model=VariableUnsetResponse,
        tags=[RouteTag.VARIABLES],
    )
    async def variables_unset(
        request: Request, execution_id: str, name: str,
    ) -> VariableUnsetResponse:
        rt = _require_system_runtime(request)
        try:
            rt.variables.unset(name, execution_id, confirm=True)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return VariableUnsetResponse()

    # -- Unified device registry ------------------------------------------
    #
    # Declared BEFORE the `/devices/{device_name}` path-param route so
    # FastAPI matches the static `/devices/registry` prefix first. Mirrors
    # a hosted REST `GET /api/devices/registry` and `/api/devices/registry/{name}`.
    # Returns the two-card view (topology + connection) plus live state and
    # per-mode eligibility, so `orca device registry list/show` produces
    # the same payload against either backend.

    async def _build_registry_view(entry) -> DeviceRegistryEntryDTO:
        summary = fault_summary(entry.name)
        is_client_connected = await entry.is_client_connected()
        is_device_connected = await entry.is_device_connected()
        is_initialized = await entry.is_initialized()
        pure_sim = await entry.supports_mode(WorkflowRunMode.PURE_SIM)
        device_sim = await entry.supports_mode(WorkflowRunMode.DEVICE_SIM)
        live = await entry.supports_mode(WorkflowRunMode.LIVE)
        return DeviceRegistryEntryDTO(
            name=entry.name,
            topology_card=entry.topology_card,
            connection_card=entry.connection_card,
            is_client_connected=is_client_connected,
            is_device_connected=is_device_connected,
            is_initialized=is_initialized,
            device_link_mode=entry.device_link_mode(),
            mode_eligibility=DeviceModeEligibilityDTO(
                pure_sim=pure_sim, device_sim=device_sim, live=live,
            ),
            fault=(
                DeviceFaultDTO.from_dc(summary) if summary is not None else None
            ),
        )

    @router.get("/devices/registry", response_model=DeviceRegistryListDTO, tags=[RouteTag.DEVICES])
    async def list_device_registry(request: Request) -> DeviceRegistryListDTO:
        rt = _require_system_runtime(request)
        entries = await rt.device_registry.list_all()
        views = [await _build_registry_view(entry) for entry in entries]
        return DeviceRegistryListDTO(devices=views)

    @router.get(
        "/devices/registry/{device_name}",
        response_model=DeviceRegistryEntryDTO,
        tags=[RouteTag.DEVICES],
    )
    async def show_device_registry_entry(
        request: Request, device_name: str,
    ) -> DeviceRegistryEntryDTO:
        rt = _require_system_runtime(request)
        entry = await rt.device_registry.get(device_name)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"device {device_name!r} is not in topology and is "
                    "not connected via gateway"
                ),
            )
        return await _build_registry_view(entry)

    # -- Device detail ----------------------------------------------------

    @router.get("/devices/{device_name}", response_model=DeviceDTO, tags=[RouteTag.DEVICES])
    async def device_info(
        request: Request, device_name: str,
    ) -> DeviceDTO:
        rt = _require_system_runtime(request)
        try:
            snap = rt.devices.get_device_status(device_name)
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )
        return DeviceDTO.from_dc(snap)

    # -- Plugins (read-only) ---------------------------------------------

    @router.get("/plugins", response_model=list[PluginDTO], tags=[RouteTag.PLUGINS])
    async def plugins_list(request: Request) -> list[PluginDTO]:
        rt = _require_system_runtime(request)
        return [PluginDTO.from_dc(p) for p in rt.list_plugins()]

    @router.get("/plugins/commands", response_model=list[PluginCommandDTO], tags=[RouteTag.PLUGINS])
    async def plugins_commands(
        request: Request,
    ) -> list[PluginCommandDTO]:
        rt = _require_system_runtime(request)
        return [
            PluginCommandDTO(
                name=c.name, description=c.description, usage=c.usage,
            )
            for c in rt.list_plugin_commands()
        ]

    # Labware reads live on the Operations surface: POST /operations/
    # {list-labware, get-labware-by-id, get-labware-by-barcode,
    # get-labware-history, get-labware-journey}.

    # -- Calibration registries -------------------------------------------
    #
    # Access-configs and deck-layouts expose full CRUD over the same runtime
    # facades both backends share. Teachpoints stay read-only here (writes
    # carry coordinate-conversion logic owned elsewhere).

    @router.get(
        "/access-configs",
        response_model=list[AccessConfigDTO],
        tags=[RouteTag.ACCESS_CONFIGS],
    )
    async def access_configs_list(request: Request) -> list[AccessConfigDTO]:
        registries = get_deployment_registries(request)
        return await registries.access_configs.list()

    @router.get(
        "/access-configs/{name}",
        response_model=AccessConfigDTO,
        tags=[RouteTag.ACCESS_CONFIGS],
    )
    async def access_configs_get(
        request: Request, name: str,
    ) -> AccessConfigDTO:
        registries = get_deployment_registries(request)
        config = await registries.access_configs.get(name)
        if config is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AccessConfig {name!r} not found",
            )
        return config

    @router.post(
        "/access-configs",
        response_model=AccessConfigDTO,
        status_code=status.HTTP_201_CREATED,
        tags=[RouteTag.ACCESS_CONFIGS],
    )
    async def access_configs_create(
        request: Request, body: AccessConfigDTO,
    ) -> AccessConfigDTO:
        registries = get_deployment_registries(request)
        try:
            await registries.access_configs.add(body, confirm=True)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc),
            )
        return body

    @router.put(
        "/access-configs/{name}",
        response_model=AccessConfigDTO,
        tags=[RouteTag.ACCESS_CONFIGS],
    )
    async def access_configs_update(
        request: Request, name: str, body: AccessConfigDTO,
    ) -> AccessConfigDTO:
        if body.name != name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"path name {name!r} does not match body name {body.name!r}",
            )
        registries = get_deployment_registries(request)
        try:
            await registries.access_configs.update(body, confirm=True)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        return body

    @router.delete(
        "/access-configs/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=[RouteTag.ACCESS_CONFIGS],
    )
    async def access_configs_delete(
        request: Request, name: str,
    ) -> None:
        registries = get_deployment_registries(request)
        try:
            deleted = await registries.access_configs.delete(name, confirm=True)
        except (ProtectedAccessConfigError, AccessConfigInUseError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc),
            )
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AccessConfig {name!r} not found",
            )

    @router.get(
        "/grip-profiles",
        response_model=list[GripProfileDTO],
        tags=[RouteTag.GRIP_PROFILES],
    )
    async def grip_profiles_list(request: Request) -> list[GripProfileDTO]:
        """Every labware type somebody has measured a grip for."""
        registries = get_deployment_registries(request)
        return await registries.grip_profiles.list()

    @router.get(
        "/grip-profiles/{labware_type}",
        response_model=GripProfileDTO,
        tags=[RouteTag.GRIP_PROFILES],
    )
    async def grip_profiles_get(
        request: Request, labware_type: str,
    ) -> GripProfileDTO:
        """One type's grip. A type nobody measured reads as an empty profile."""
        registries = get_deployment_registries(request)
        return await registries.grip_profiles.get(labware_type)

    @router.patch(
        "/grip-profiles/{labware_type}",
        response_model=GripProfileDTO,
        tags=[RouteTag.GRIP_PROFILES],
    )
    async def grip_profiles_patch(
        request: Request, labware_type: str, body: GripProfilePatchRequest,
    ) -> GripProfileDTO:
        """Change the fields named and leave the rest inheriting."""
        registries = get_deployment_registries(request)
        try:
            return await registries.grip_profiles.apply(
                labware_type, body.set, body.clear, confirm=True,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            )

    @router.delete(
        "/grip-profiles/{labware_type}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=[RouteTag.GRIP_PROFILES],
    )
    async def grip_profiles_reset(request: Request, labware_type: str) -> None:
        """Discard everything measured for this type."""
        registries = get_deployment_registries(request)
        if not await registries.grip_profiles.reset(labware_type, confirm=True):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"labware type {labware_type!r} has no grip profile",
            )

    @router.get(
        "/move-defaults",
        response_model=list[MoveDefaultsDTO],
        tags=[RouteTag.MOVE_DEFAULTS],
    )
    async def move_defaults_list(request: Request) -> list[MoveDefaultsDTO]:
        """Every arm's starting numbers: the tuned ones, plus the mounted ones."""
        registries = get_deployment_registries(request)
        rt = request.app.state.system_runtime
        return await registries.move_defaults.list(
            rt.system if rt is not None else None,
        )

    @router.get(
        "/move-defaults/{transporter_name}",
        response_model=MoveDefaultsDTO,
        tags=[RouteTag.MOVE_DEFAULTS],
    )
    async def move_defaults_get(
        request: Request, transporter_name: str,
    ) -> MoveDefaultsDTO:
        """One arm's starting numbers. An arm nobody tuned reads as the seed."""
        registries = get_deployment_registries(request)
        return await registries.move_defaults.get(transporter_name)

    @router.patch(
        "/move-defaults/{transporter_name}",
        response_model=MoveDefaultsDTO,
        tags=[RouteTag.MOVE_DEFAULTS],
    )
    async def move_defaults_patch(
        request: Request, transporter_name: str, body: MoveDefaultsPatchRequest,
    ) -> MoveDefaultsDTO:
        """Change the fields named and leave the rest alone."""
        registries = get_deployment_registries(request)
        try:
            return await registries.move_defaults.apply(
                transporter_name, body.set, body.clear, confirm=True,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            )

    @router.delete(
        "/move-defaults/{transporter_name}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=[RouteTag.MOVE_DEFAULTS],
    )
    async def move_defaults_reset(
        request: Request, transporter_name: str,
    ) -> None:
        """Discard everything tuned for this arm and put it back on the seed."""
        registries = get_deployment_registries(request)
        if not await registries.move_defaults.reset(transporter_name, confirm=True):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"transporter {transporter_name!r} has no tuned move defaults",
            )

    @router.get(
        "/teachpoints",
        response_model=list[TeachpointDTO],
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_list_all(
        request: Request,
    ) -> list[TeachpointDTO]:
        """Cross-transporter teachpoint enumeration.

        Mirrors ``labware list`` / ``device list`` -- no args yields
        every teachpoint across every transporter, each row carrying
        its own ``device_id`` so callers can re-group client-side.
        """
        rt = _require_system_runtime(request)
        rows = await rt.teachpoints.list_all()
        return [TeachpointDTO.from_value(device_id, tp) for device_id, tp in rows]

    @router.get(
        "/teachpoints/{device_id}",
        response_model=list[TeachpointDTO],
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_list(
        request: Request, device_id: str,
    ) -> list[TeachpointDTO]:
        rt = _require_system_runtime(request)
        try:
            tps = await rt.teachpoints.list(device_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        return [TeachpointDTO.from_value(device_id, tp) for tp in tps]

    @router.get(
        "/teachpoints/{device_id}/{name:path}",
        response_model=TeachpointDTO,
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_get(
        request: Request, device_id: str, name: str,
    ) -> TeachpointDTO:
        rt = _require_system_runtime(request)
        try:
            tp = await rt.teachpoints.get(device_id, name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        if tp is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Teachpoint {name!r} not found for device {device_id!r}",
            )
        return TeachpointDTO.from_value(device_id, tp)

    async def _resolve_access(
        request: Request, access_config_name: str | None,
    ) -> AccessConfig | None:
        if access_config_name is None:
            return None
        access = await get_deployment_registries(request).access_configs.get(
            access_config_name,
        )
        if access is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"AccessConfig {access_config_name!r} not found; "
                    "create it via /access-configs first."
                ),
            )
        return access

    @router.patch(
        "/teachpoints/{device_id}/{position_id}/labware/{labware_type}",
        response_model=TeachpointDTO,
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_set_labware_override(
        request: Request, device_id: str, position_id: str, labware_type: str,
        body: GripProfilePatchRequest,
    ) -> TeachpointDTO:
        """Make one labware an exception at this one position."""
        rt = _require_system_runtime(request)
        try:
            tp = await rt.teachpoints.apply_labware_override(
                device_id, position_id, labware_type,
                body.set, body.clear, confirm=True,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            )
        return TeachpointDTO.from_value(device_id, tp)

    @router.delete(
        "/teachpoints/{device_id}/{position_id}/labware/{labware_type}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_clear_labware_override(
        request: Request, device_id: str, position_id: str, labware_type: str,
    ) -> None:
        """Hand this labware back to how the position handles everything else."""
        rt = _require_system_runtime(request)
        try:
            dropped = await rt.teachpoints.clear_labware_override(
                device_id, position_id, labware_type, confirm=True,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        if not dropped:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Teachpoint {position_id!r} on device {device_id!r} has no "
                    f"override for labware {labware_type!r}"
                ),
            )

    @router.post(
        "/teachpoints",
        response_model=TeachpointDTO,
        status_code=status.HTTP_201_CREATED,
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_create(
        request: Request, body: CreateTeachpointRequest,
    ) -> TeachpointDTO:
        try:
            validate_coords(body.coord_type, body.coords)
        except InvalidTeachpointCoordsError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            )
        access = await _resolve_access(request, body.access_config_name)
        try:
            tp = build_teachpoint(
                coord_type=body.coord_type,
                position_id=body.position_id,
                coords=body.coords,
                access=access,
                orientation=body.orientation,
                gateway=body.gateway,
                taught_with=body.taught_with,
                by_labware=body.by_labware,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=message_of(exc),
            )
        rt = _require_system_runtime(request)
        try:
            await rt.teachpoints.add(body.device_id, tp, confirm=True)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc),
            )
        return TeachpointDTO.from_value(body.device_id, tp)

    @router.put(
        "/teachpoints/{device_id}/{name:path}",
        response_model=TeachpointDTO,
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_update(
        request: Request, device_id: str, name: str,
        body: UpdateTeachpointRequest,
    ) -> TeachpointDTO:
        rt = _require_system_runtime(request)
        try:
            existing = await rt.teachpoints.get(device_id, name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Teachpoint {name!r} not found for device {device_id!r}",
            )

        coord_type = coord_type_for(existing)
        try:
            validate_coords(coord_type, body.coords)
        except InvalidTeachpointCoordsError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            )

        access_config_name = (
            body.access_config_name
            if body.update_access_config
            else existing.access_config_name
        )
        gateway = body.gateway if body.update_gateway else existing.gateway
        orientation = (
            body.orientation if body.update_orientation else existing.orientation
        )
        taught_with = (
            body.taught_with if body.update_taught_with else existing.taught_with
        )
        access = await _resolve_access(request, access_config_name)
        try:
            tp = build_teachpoint(
                coord_type=coord_type,
                position_id=name,
                coords=body.coords,
                access=access,
                orientation=orientation,
                gateway=gateway,
                taught_with=taught_with,
                by_labware=existing.by_labware,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=message_of(exc),
            )
        try:
            await rt.teachpoints.update(device_id, tp, confirm=True)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        return TeachpointDTO.from_value(device_id, tp)

    @router.delete(
        "/teachpoints/{device_id}/{name:path}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=[RouteTag.TEACHPOINTS],
    )
    async def teachpoints_delete(
        request: Request, device_id: str, name: str,
    ) -> None:
        rt = _require_system_runtime(request)
        try:
            deleted = await rt.teachpoints.delete(device_id, name, confirm=True)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Teachpoint {name!r} not found for device {device_id!r}",
            )

    @router.get(
        "/deck-layouts",
        response_model=list[DeckLayoutSummaryDTO],
        tags=[RouteTag.DECK_LAYOUTS],
    )
    async def deck_layouts_list_all(
        request: Request,
    ) -> list[DeckLayoutSummaryDTO]:
        """Cross-LH deck-layout enumeration."""
        rt = _require_system_runtime(request)
        rows = await rt.deck_layouts.list_all()
        return [
            DeckLayoutSummaryDTO(
                device_id=device_id, name=name, deck_type=config.deck_type,
            )
            for device_id, name, config in rows
        ]

    @router.get(
        "/deck-layouts/{device_id}",
        response_model=list[DeckLayoutSummaryDTO],
        tags=[RouteTag.DECK_LAYOUTS],
    )
    async def deck_layouts_list(
        request: Request, device_id: str,
    ) -> list[DeckLayoutSummaryDTO]:
        rt = _require_system_runtime(request)
        try:
            rows = await rt.deck_layouts.list(device_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        return [
            DeckLayoutSummaryDTO(
                device_id=device_id, name=name, deck_type=config.deck_type,
            )
            for name, config in rows
        ]

    @router.get(
        "/deck-layouts/{device_id}/{name}",
        response_model=DeckLayoutDTO,
        tags=[RouteTag.DECK_LAYOUTS],
    )
    async def deck_layouts_get(
        request: Request, device_id: str, name: str,
    ) -> DeckLayoutDTO:
        rt = _require_system_runtime(request)
        try:
            config = await rt.deck_layouts.get(device_id, name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        if config is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"DeckLayout {name!r} not found for device {device_id!r}",
            )
        return DeckLayoutDTO(
            device_id=device_id, name=name,
            config=config,
        )

    @router.post(
        "/deck-layouts/{device_id}/{name}",
        response_model=DeckLayoutDTO,
        status_code=status.HTTP_201_CREATED,
        tags=[RouteTag.DECK_LAYOUTS],
    )
    async def deck_layouts_create(
        request: Request, device_id: str, name: str, body: DeckLayoutConfig,
    ) -> DeckLayoutDTO:
        rt = _require_system_runtime(request)
        try:
            await rt.deck_layouts.add(device_id, name, body, confirm=True)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        return DeckLayoutDTO(device_id=device_id, name=name, config=body)

    @router.put(
        "/deck-layouts/{device_id}/{name}",
        response_model=DeckLayoutDTO,
        tags=[RouteTag.DECK_LAYOUTS],
    )
    async def deck_layouts_update(
        request: Request, device_id: str, name: str, body: DeckLayoutConfig,
    ) -> DeckLayoutDTO:
        rt = _require_system_runtime(request)
        try:
            await rt.deck_layouts.update(device_id, name, body, confirm=True)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        return DeckLayoutDTO(device_id=device_id, name=name, config=body)

    @router.delete(
        "/deck-layouts/{device_id}/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=[RouteTag.DECK_LAYOUTS],
    )
    async def deck_layouts_delete(
        request: Request, device_id: str, name: str,
    ) -> None:
        rt = _require_system_runtime(request)
        try:
            deleted = await rt.deck_layouts.delete(device_id, name, confirm=True)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(exc),
            )
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"DeckLayout {name!r} not found for device {device_id!r}",
            )

    # Labware writes live on the Operations surface: POST /operations/
    # {edit-labware-location, edit-labware-barcode, reset-labware-location,
    # register-labware}.

    # -- Labware catalog (operator CRUD via deployment_registries) ---------
    # Deployment-scoped: works with no system mounted. Same facade both
    # backends route through; a hosted deployment injects a Postgres store, the daemon
    # defaults to the in-memory seed store.

    @router.get(
        "/labware", response_model=LabwareCatalogListResponse,
        tags=[RouteTag.LABWARE],
    )
    async def list_labware_catalog(
        request: Request, category: str | None = Query(default=None),
    ) -> LabwareCatalogListResponse:
        registries = get_deployment_registries(request)
        rows = await registries.labware_catalog.list(category)
        return LabwareCatalogListResponse(
            labware=[r.to_summary() for r in rows],
        )

    @router.get(
        "/labware/{labware_type}", response_model=LabwareCatalogEntry,
        tags=[RouteTag.LABWARE],
    )
    async def get_labware_catalog_entry(
        request: Request, labware_type: str,
    ) -> LabwareCatalogEntry:
        registries = get_deployment_registries(request)
        try:
            return await registries.labware_catalog.get(labware_type)
        except LabwareNotFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )

    @router.post(
        "/labware", response_model=LabwareCatalogEntry,
        status_code=status.HTTP_201_CREATED, tags=[RouteTag.LABWARE],
    )
    async def add_labware_catalog_entry(
        request: Request, body: LabwareCatalogCreateRequest,
    ) -> LabwareCatalogEntry:
        registries = get_deployment_registries(request)
        entry = LabwareCatalogEntry(
            labware_type=body.labware_type, display_name=body.display_name,
            category=body.category, vendor=body.vendor,
            source=OPERATOR_CUSTOM_SOURCE, geometry=body.geometry,
            plr_class_name=body.plr_class_name,
        )
        try:
            return await registries.labware_catalog.add(entry)
        except LabwareCatalogConflict as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(e),
            )
        except LabwareGeometryInvalid as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e),
            )

    @router.put(
        "/labware/{labware_type}", response_model=LabwareCatalogEntry,
        tags=[RouteTag.LABWARE],
    )
    async def update_labware_catalog_entry(
        request: Request, labware_type: str,
        body: LabwareCatalogUpdateRequest,
    ) -> LabwareCatalogEntry:
        registries = get_deployment_registries(request)
        entry = LabwareCatalogEntry(
            labware_type=labware_type, display_name=body.display_name,
            category=body.category, vendor=body.vendor,
            source=OPERATOR_CUSTOM_SOURCE, geometry=body.geometry,
            plr_class_name=body.plr_class_name,
        )
        try:
            return await registries.labware_catalog.update(entry)
        except LabwareNotFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )
        except SeedLabwareReadOnly as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(e),
            )
        except LabwareGeometryInvalid as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e),
            )

    @router.delete(
        "/labware/{labware_type}",
        status_code=status.HTTP_204_NO_CONTENT, tags=[RouteTag.LABWARE],
    )
    async def delete_labware_catalog_entry(
        request: Request, labware_type: str,
    ) -> None:
        registries = get_deployment_registries(request)
        try:
            await registries.labware_catalog.delete(labware_type)
        except LabwareNotFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )
        except SeedLabwareReadOnly as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(e),
            )

    # -- Device writes + capabilities -------------------------------------

    @router.get(
        "/devices/{device_name}/capabilities",
        response_model=list[CommandDescriptorDTO],
        tags=[RouteTag.DEVICES],
    )
    async def device_capabilities(
        request: Request, device_name: str,
    ) -> list[CommandDescriptorDTO]:
        rt = _require_system_runtime(request)
        try:
            commands = rt.devices.get_supported_commands(device_name)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return [
            CommandDescriptorDTO(
                device_name=c.device_name,
                capability=c.capability,
                danger_level=c.danger_level.name,
                description=c.description,
                cli_accessible=c.cli_accessible,
                params=tuple(
                    ParamSpecDTO(
                        name=p.name, type_name=p.type_name,
                        required=p.required, default=p.default,
                        description=p.description,
                    )
                    for p in c.params
                ),
            )
            for c in commands
        ]

    @router.get(
        "/devices/{device_name}/introspection",
        response_model=DeviceIntrospectionDTO,
        tags=[RouteTag.DEVICES],
    )
    async def device_introspection(
        request: Request, device_name: str,
    ) -> DeviceIntrospectionDTO:
        rt = _require_system_runtime(request)
        try:
            info = rt.devices.get_device_introspection(device_name)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        return DeviceIntrospectionDTO(
            type=info.type,
            name=info.name,
            interfaces=info.interfaces,
            capabilities=info.capabilities,
            provides_state=info.provides_state,
            methods=info.methods,
        )

    @router.post(
        "/devices/{device_name}/execute",
        response_model=DeviceInvocationResultDTO,
        tags=[RouteTag.DEVICES],
    )
    async def device_execute(
        request: Request, device_name: str, body: DeviceExecuteRequest,
    ) -> DeviceInvocationResultDTO:
        rt = _require_system_runtime(request)
        try:
            result = await rt.devices.execute(
                device_name, body.command,
                options=body.options, mode=body.mode, confirm=True,
                vendor_confirm=body.confirm,
            )
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=message_of(e),
            )
        except DeviceError as e:
            raise _gateway_http_error(e)
        return DeviceInvocationResultDTO(
            success=result.success,
            value_type=result.value_type,
            value=result.value,
            duration_seconds=result.duration_seconds,
            device_name=result.device_name,
            command_or_capability=result.command_or_capability,
        )

    @router.post(
        "/devices/{device_name}/invoke",
        response_model=DeviceInvocationResultDTO,
        tags=[RouteTag.DEVICES],
    )
    async def device_invoke(
        request: Request, device_name: str, body: DeviceInvokeRequest,
    ) -> DeviceInvocationResultDTO:
        rt = _require_system_runtime(request)
        try:
            result = await rt.devices.invoke(
                device_name, body.capability,
                kwargs=body.kwargs or {}, mode=body.mode, confirm=True,
                vendor_confirm=body.confirm,
            )
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=message_of(e),
            )
        except DeviceError as e:
            raise _gateway_http_error(e)
        return DeviceInvocationResultDTO(
            success=result.success,
            value_type=result.value_type,
            value=result.value,
            duration_seconds=result.duration_seconds,
            device_name=result.device_name,
            command_or_capability=result.command_or_capability,
        )

    @router.post(
        "/devices/{device_name}/initialize",
        response_model=DeviceInitializeResponse,
        tags=[RouteTag.DEVICES],
    )
    async def device_initialize(
        request: Request, device_name: str,
        body: DeviceLifecycleRequest | None = None,
    ) -> DeviceInitializeResponse:
        rt = _require_system_runtime(request)
        try:
            await rt.devices.initialize(
                device_name, mode=body.mode if body else None, confirm=True,
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        return DeviceInitializeResponse()

    @router.post(
        "/devices/{device_name}/connect",
        response_model=DeviceConnectResponse,
        tags=[RouteTag.DEVICES],
    )
    async def device_connect(
        request: Request, device_name: str,
        body: DeviceLifecycleRequest | None = None,
    ) -> DeviceConnectResponse:
        rt = _require_system_runtime(request)
        try:
            await rt.devices.connect(
                device_name, mode=body.mode if body else None,
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        return DeviceConnectResponse()

    @router.post(
        "/devices/{device_name}/disconnect",
        response_model=DeviceDisconnectResponse,
        tags=[RouteTag.DEVICES],
    )
    async def device_disconnect(
        request: Request, device_name: str,
        body: DeviceLifecycleRequest | None = None,
    ) -> DeviceDisconnectResponse:
        rt = _require_system_runtime(request)
        try:
            await rt.devices.disconnect(
                device_name, mode=body.mode if body else None, confirm=True,
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        return DeviceDisconnectResponse()

    # -- Submissions ------------------------------------------------------
    #
    # Every submission URL lives on the unified `/operations/*` surface.
    # The legacy decorator handlers
    # (``POST /submissions``, ``GET /submissions``, ``GET
    # /submissions/{id}``, ``POST /executions/{id}/close``) were deleted
    # in lockstep with this binder migration.

    def _resolve_runtime_for_op(request: Request) -> SystemRuntime:
        rt = request.app.state.system_runtime
        if rt is None:
            raise OperationError.service_unavailable("no system loaded")
        return rt

    bind_orca_rest(
        router,
        path="/operations/submit-execution",
        method="POST",
        op_factory=lambda req: SubmitExecutionOperation(
            runtime=_resolve_runtime_for_op(req),
        ),
        request_model=SubmitExecutionRequest,
        response_model=SubmitExecutionResponse,
        tags=[RouteTag.SUBMISSIONS],
    )

    bind_orca_rest(
        router,
        path="/operations/list-submissions",
        method="POST",
        op_factory=lambda req: ListSubmissionsOperation(
            runtime=_resolve_runtime_for_op(req),
        ),
        request_model=ListSubmissionsRequest,
        response_model=ListSubmissionsResponse,
        tags=[RouteTag.SUBMISSIONS],
    )

    bind_orca_rest(
        router,
        path="/operations/get-submission",
        method="POST",
        op_factory=lambda req: GetSubmissionOperation(
            runtime=_resolve_runtime_for_op(req),
        ),
        request_model=GetSubmissionRequest,
        response_model=GetSubmissionResponse,
        tags=[RouteTag.SUBMISSIONS],
    )

    # -- Reservation cancel ---------------------------
    #
    # Removal lives at POST /operations/remove-execution.

    @router.delete(
        "/executions/{execution_id}/reservations/{reservation_id}",
        response_model=ReservationCancelResponse,
        tags=[RouteTag.RESERVATIONS],
    )
    async def reservation_cancel(
        request: Request, execution_id: str, reservation_id: str,
        reason: str | None = Query(
            None,
            description="Operator-supplied justification for the cancel. "
                        "Required: @dangerous audits this action.",
        ),
    ) -> ReservationCancelResponse:
        rt = _require_system_runtime(request)
        try:
            await rt.registry.cancel_reservation(
                execution_id, reservation_id,
                reason=reason, confirm=True,
            )
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e),
            )
        except ValueError as e:
            # @dangerous raises ValueError when requires_reason is unmet.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(e),
            )
        return ReservationCancelResponse()

    # -- Incidents --------------------------------------------------------

    @router.get("/incidents", response_model=list[IncidentDTO], tags=[RouteTag.INCIDENTS])
    async def incidents_list(
        request: Request,
        unacknowledged_only: bool = Query(False),
        category: str | None = Query(None),
        execution_id: str | None = Query(None),
    ) -> list[IncidentDTO]:
        from orca.runtime.incident_store import IncidentCategory
        rt = _require_system_runtime(request)
        cat = None
        if category is not None:
            try:
                cat = IncidentCategory[category.upper()]
            except KeyError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"unknown incident category {category!r}; valid: "
                        f"{[c.name for c in IncidentCategory]}"
                    ),
                )
        items = await rt.incidents.list(
            unacknowledged_only=unacknowledged_only,
            category=cat,
            execution_id=execution_id,
        )
        return [IncidentDTO.from_incident(i) for i in items]

    @router.get("/incidents/{incident_id}", response_model=IncidentDTO, tags=[RouteTag.INCIDENTS])
    async def incidents_get(
        request: Request, incident_id: str,
    ) -> IncidentDTO:
        rt = _require_system_runtime(request)
        try:
            inc = await rt.incidents.get(incident_id)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"incident {incident_id!r} not found",
            )
        return IncidentDTO.from_incident(inc)

    @router.post(
        "/incidents/{incident_id}/ack",
        response_model=IncidentAckResponse,
        tags=[RouteTag.INCIDENTS],
    )
    async def incidents_ack(
        request: Request, incident_id: str,
    ) -> IncidentAckResponse:
        rt = _require_system_runtime(request)
        try:
            await rt.incidents.acknowledge(incident_id, confirm=True)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"incident {incident_id!r} not found",
            )
        return IncidentAckResponse(acknowledged_count=1)

    @router.post("/incidents/ack-all", response_model=IncidentAckResponse, tags=[RouteTag.INCIDENTS])
    async def incidents_ack_all(
        request: Request,
        category: str | None = Query(None),
    ) -> IncidentAckResponse:
        from orca.runtime.incident_store import IncidentCategory
        rt = _require_system_runtime(request)
        cat = None
        if category is not None:
            try:
                cat = IncidentCategory[category.upper()]
            except KeyError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"unknown incident category {category!r}; valid: "
                        f"{[c.name for c in IncidentCategory]}"
                    ),
                )
        n = await rt.incidents.acknowledge_all(category=cat, confirm=True)
        return IncidentAckResponse(acknowledged_count=n)

    # -- Recoverable-timeout operator decisions ---------------------------
    # The engine owns the held device call; these resolve it. Parity with
    # a hosted deployment's /api/incidents/{id}/recoverable_timeout/* surface.

    @router.post(
        "/incidents/{incident_id}/recoverable_timeout/extend",
        response_model=RecoverableTimeoutDecisionResponse,
        tags=[RouteTag.INCIDENTS],
    )
    async def recoverable_timeout_extend(
        request: Request, incident_id: str,
        body: RecoverableTimeoutExtendRequest,
    ) -> RecoverableTimeoutDecisionResponse:
        rt = _require_system_runtime(request)
        try:
            rt.recoverable_timeout_extend(incident_id, body.additional_seconds)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no held command for recoverable-timeout incident {incident_id!r}",
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e),
            )
        return RecoverableTimeoutDecisionResponse(
            incident_id=incident_id, decision="extend",
        )

    @router.post(
        "/incidents/{incident_id}/recoverable_timeout/abort",
        response_model=RecoverableTimeoutDecisionResponse,
        tags=[RouteTag.INCIDENTS],
    )
    async def recoverable_timeout_abort(
        request: Request, incident_id: str,
        body: RecoverableTimeoutOperatorRequest,
    ) -> RecoverableTimeoutDecisionResponse:
        rt = _require_system_runtime(request)
        try:
            rt.recoverable_timeout_abort(
                incident_id, body.operator_name, body.reason,
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no held command for recoverable-timeout incident {incident_id!r}",
            )
        return RecoverableTimeoutDecisionResponse(
            incident_id=incident_id, decision="abort",
        )

    @router.post(
        "/incidents/{incident_id}/recoverable_timeout/mark_complete",
        response_model=RecoverableTimeoutDecisionResponse,
        tags=[RouteTag.INCIDENTS],
    )
    async def recoverable_timeout_mark_complete(
        request: Request, incident_id: str,
        body: RecoverableTimeoutOperatorRequest,
    ) -> RecoverableTimeoutDecisionResponse:
        rt = _require_system_runtime(request)
        try:
            rt.recoverable_timeout_mark_complete(
                incident_id, body.operator_name, body.reason,
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no held command for recoverable-timeout incident {incident_id!r}",
            )
        return RecoverableTimeoutDecisionResponse(
            incident_id=incident_id, decision="mark_complete",
        )

    # -- Registry (read-only system inventory) ----------------------------

    @router.get("/system", response_model=SystemInfoDTO, tags=[RouteTag.SYSTEM])
    async def system_info(request: Request) -> SystemInfoDTO:
        rt = _require_system_runtime(request)
        return SystemInfoDTO.from_dc(rt.registry.system_info())

    @router.get(
        "/catalog/workflows",
        response_model=list[WorkflowTemplateDTO],
        tags=[RouteTag.CATALOG],
    )
    async def list_workflows(request: Request) -> list[WorkflowTemplateDTO]:
        rt = _require_system_runtime(request)
        return [WorkflowTemplateDTO.from_dc(w) for w in rt.registry.list_workflow_templates()]

    @router.get(
        "/catalog/methods",
        response_model=list[MethodTemplateDTO],
        tags=[RouteTag.CATALOG],
    )
    async def list_methods(request: Request) -> list[MethodTemplateDTO]:
        rt = _require_system_runtime(request)
        return [MethodTemplateDTO.from_dc(m) for m in rt.registry.list_method_templates()]

    @router.get(
        "/catalog/threads",
        response_model=list[ThreadTemplateDTO],
        tags=[RouteTag.CATALOG],
    )
    async def list_thread_templates(request: Request) -> list[ThreadTemplateDTO]:
        rt = _require_system_runtime(request)
        return [ThreadTemplateDTO.from_dc(t) for t in rt.registry.list_thread_templates()]

    @router.get(
        "/catalog/locations",
        response_model=list[LocationDTO],
        tags=[RouteTag.CATALOG],
    )
    async def list_locations(request: Request) -> list[LocationDTO]:
        rt = _require_system_runtime(request)
        return [LocationDTO.from_dc(loc) for loc in rt.registry.list_locations()]

    @router.get(
        "/catalog/devices",
        response_model=list[DeviceDTO],
        tags=[RouteTag.CATALOG],
    )
    async def list_devices(request: Request) -> list[DeviceDTO]:
        rt = _require_system_runtime(request)
        return [DeviceDTO.from_dc(d) for d in rt.registry.list_devices()]

    # -- Topology view -----------------------------------------------------

    @router.get("/topology", response_model=None, tags=[RouteTag.TOPOLOGY])
    async def topology_view(
        request: Request, source: bool = Query(default=False),
    ) -> TopologyViewResponse | TopologySourceResponse:
        """Composed live topology, or (``?source=true``) the factory spec the
        runtime was mounted from.

        The local daemon is factory-spec backed, so ``source`` returns that
        spec string (the local source-of-truth) rather than a topology.py blob.
        """
        rt = _require_system_runtime(request)
        if source:
            return TopologySourceResponse(source=str(request.app.state.spec or ""))
        registry = rt.registry
        return TopologyViewResponse(
            devices=[DeviceDTO.from_dc(d) for d in registry.list_devices()],
            transporters=[TransporterDTO.from_dc(t) for t in registry.list_transporters()],
            movers=[MoverDTO.from_dc(m) for m in registry.list_movers()],
            resource_pools=[ResourcePoolDTO.from_dc(p) for p in registry.list_resource_pools()],
            locations=[LocationDTO.from_dc(loc) for loc in registry.list_locations()],
            labware_templates=[
                LabwareTemplateDTO.from_dc(lt) for lt in registry.list_labware_templates()
            ],
        )

    # -- Runtime status (diagnostic; tolerates an unmounted runtime) -------

    @router.get("/runtime/status", response_model=RuntimeStatusResponse, tags=[RouteTag.RUNTIME])
    async def runtime_status(request: Request) -> RuntimeStatusResponse:
        """Whether a SystemRuntime is mounted. Unlike most routes this never
        409s -- it is the diagnostic an operator calls when the runtime is
        not ready. The daemon has no submission pipeline, so there is no
        build-error to report."""
        rt: SystemRuntime | None = request.app.state.system_runtime
        return RuntimeStatusResponse(built=rt is not None, last_build_error=None)

    # -- Labware runtime mutations (clear / discharge) ---------------------

    @router.post(
        "/labware/runtime/clear-submission",
        response_model=LabwareClearSubmissionResponse,
        tags=[RouteTag.LABWARE],
    )
    async def labware_clear_submission(
        request: Request,
        submission_id: str = Query(...),
        body: dict[str, bool] | None = None,
    ) -> LabwareClearSubmissionResponse:
        rt = _require_system_runtime(request)
        force = bool(body.get("force", False)) if body else False
        try:
            result = await rt.labware.clear_submission_labware(
                submission_id, force=force,
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        except (RuntimeError, ValueError) as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        return LabwareClearSubmissionResponse(
            cleared=list(result.cleared),
            preserved_reuse_bound=list(result.preserved_reuse_bound),
        )

    @router.post(
        "/labware/runtime/discharge",
        response_model=LabwareClearResponse,
        tags=[RouteTag.LABWARE],
    )
    async def labware_discharge(
        request: Request,
        labware_id: str = Query(...),
        body: dict[str, bool] | None = None,
    ) -> LabwareClearResponse:
        rt = _require_system_runtime(request)
        force = bool(body.get("force", False)) if body else False
        try:
            await rt.labware.discharge_labware(labware_id, force=force)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message_of(e))
        except (RuntimeError, ValueError) as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        return LabwareClearResponse(cleared_labware_ids=[labware_id])

    @router.post(
        "/labware/runtime/clear-all",
        response_model=LabwareClearResponse,
        tags=[RouteTag.LABWARE],
    )
    async def labware_clear_all(
        request: Request,
        body: dict[str, bool] | None = None,
    ) -> LabwareClearResponse:
        rt = _require_system_runtime(request)
        force = bool(body.get("force", False)) if body else False
        try:
            cleared = await rt.labware.clear_all_labware(force=force)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        return LabwareClearResponse(cleared_labware_ids=list(cleared))

    # -- Events ------------------------------------------------------------

    @router.get("/events", tags=[RouteTag.EVENTS])
    async def events_since(
        request: Request,
        since: float = Query(
            0.0, ge=0.0,
            description="Unix timestamp; only events at or after this time.",
        ),
        execution_id: str | None = Query(
            None,
            description="If set, filter to this execution only.",
        ),
    ) -> list[dict[str, JsonValue]]:
        """Poll events emitted since `since`. For live streaming use /events/stream."""
        rt = _require_system_runtime(request)
        if execution_id is not None:
            events = rt.get_events_for_execution(execution_id)
            events = [e for e in events if e.timestamp >= since]
        else:
            events = rt.get_events_since(since)
        return [e.to_dict() for e in events]

    @router.get("/events/stream", tags=[RouteTag.EVENTS])
    async def events_stream(
        request: Request,
        execution_id: str | None = Query(
            None,
            description="Server-side filter to a single execution.",
        ),
    ) -> EventSourceResponse:
        """Server-Sent Events stream. Subscribes to the daemon's event sink
        and yields each event until the client disconnects.

        The sink persists across load/unload, so this connection keeps
        working if the operator unloads and loads a different topology
        (events from the new topology flow into the same subscription).
        """
        sink = request.app.state.event_sink
        subscription = sink.subscribe()

        async def generator():
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(
                            subscription.queue.get(), timeout=1.0,
                        )
                    except asyncio.TimeoutError:
                        # Heartbeat poll: keep-alive for the connection and
                        # re-check is_disconnected on the next loop.
                        continue
                    if (
                        execution_id is not None
                        and event.execution_id != execution_id
                    ):
                        continue
                    yield {"event": "runtime_event", "data": event.to_dict()}
            finally:
                subscription.unsubscribe()

        return EventSourceResponse(generator())

    return router
