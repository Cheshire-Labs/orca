"""LocalDaemonClient: thin HTTP client the CLI verbs use to dial the daemon.

Reads `~/.orca/daemon.json` (or the tmp override) to find the port,
then `httpx.Client`-based calls for each HTTP route. HTTP errors
map to CLI exit codes so the shell reports the right signal:

    404 -> EXIT_NOT_FOUND   (unknown execution / thread id)
    409 -> EXIT_CONFLICT    (no system loaded, already loaded, state error)
    400 -> EXIT_USAGE       (malformed request / invalid decision)
    5xx -> EXIT_GENERIC

Clean failure when no daemon is running: daemon.json absent -> exit code 10
(EXIT_NOT_CONNECTED) with a message pointing the operator at `orca start`.

`LocalDaemonClient` satisfies `IControlPlaneClient` for the overlapping command
surface (executions, devices list, introspection, health); local-only methods
(threads, variables, incidents, submissions, plugins, reservations, registry
catalog reads, device invoke/execute/initialize, the rich device snapshot list)
stay on this class only and are dispatched by `local_only`-tagged CLI verbs
that bypass the Protocol-shaped wrapper.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import httpx
import typer
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from pydantic import JsonValue

from orca.runtime.run_modes import WorkflowRunMode
from orca.cli import output
from orca.cli.control_plane import (
    AuditEntryDTO as ControlPlaneAuditEntryDTO,
    DeviceDTO as ControlPlaneDeviceDTO,
    DeviceIntrospectionDTO as ControlPlaneDeviceIntrospectionDTO,
    DeviceRegistryEntryDTO as ControlPlaneDeviceRegistryEntryDTO,
    ExecutionDetailDTO as ControlPlaneExecutionDetailDTO,
    ExecutionRecordDTO as ControlPlaneExecutionRecordDTO,
    MoverHoldReleaseDTO,
    LabwareCatalogEntryDTO,
    LabwareCatalogResponseDTO,
    LabwareCatalogSummaryDTO,
    LabwareClearResponseDTO,
    LabwareClearSubmissionResponseDTO,
    LabwareJourneyResponseDTO,
    MethodSummaryDTO,
    OpsHistoryGetResponseDTO,
    OpsHistorySearchResponseDTO,
    RuntimeStatusResponseDTO,
    StopExecutionResultDTO,
    TopologySourceDTO,
    TopologyViewDTO,
    WorkflowSummaryDTO,
)
from orca.daemon.lifecycle import (
    DaemonInfo,
    detect_live_daemon,
    pid_file_path as _default_pid_file_path,
)
from orca.operations.submission_models import SubmitExecutionResponse
from orca.daemon.schemas import (
    AccessConfigDTO,
    GripProfileDTO,
    GripProfilePatchRequest,
    MoveDefaultsDTO,
    MoveDefaultsPatchRequest,
    CommandDescriptorDTO,
    DeckLayoutDTO,
    DeckLayoutSummaryDTO,
    DeviceDTO,
    DeviceExecuteRequest,
    DeviceInvocationResultDTO,
    DeviceInvokeRequest,
    ExecutionDetailDTO,
    ExecutionRecordDTO,
    IncidentAckAllQuery,
    IncidentAckResponse,
    RecoverableTimeoutDecisionResponse,
    IncidentDTO,
    IncidentListQuery,
    LabwareCatalogCreateRequest,
    LabwareCatalogUpdateRequest,
    LabwareDTO,
    LocationDTO,
    LocationEventDTO,
    MountTopologyRequest,
    MountTopologyResponse,
    RegisterWorkflowRequest,
    RegisterWorkflowResponse,
    TeachpointDTO,
    CrossExecVariableDTO,
    OptionValueJson,
    PluginCommandDTO,
    PluginDTO,
    ReservationCancelQuery,
    ReservationSnapshotDTO,
    ResumeAllResultDTO,
    RunModeStr,
    SubmissionCloseResponse,
    SubmissionDTO,
    SubmissionSubmitRequest,
    SubmitMethodExecutionRequest,
    SubmitWorkflowRequest,
    SystemInfoDTO,
    ThreadSnapshotDTO,
    ThreadTemplateDTO,
    VariableResolutionResponse,
    VariableSetRequest,
    VariableSetResponse,
    VariableValue,
    VariablesListResponse,
    WorkflowTemplateDTO,
)
from orca.operations._scope import ExecutionScope, ThreadScope
from orca.operations.state_models import UnsettledStateResponse
from orca.operations.device_models import (
    CompareDeckRequest,
    ConfirmMountedTipsRequest,
    GetMountedTipsRequest,
    GetMountedTipsResponse,
    MountedTipDTO,
    SetMountedTipsRequest,
    CompareDeckResponse,
    ReconcileDeckRequest,
    ClearDeviceFaultRequest,
    ClearDeviceFaultResponse,
    ReleaseDeviceControlRequest,
    TakeDeviceControlRequest,
    ReconcileDeckResponse,
)
from orca.runtime.runtime_state import RuntimeState
from orca.operations.execution_models import (
    CloseExecutionRequest,
    GetExecutionDetailRequest,
    RemoveExecutionRequest,
    StopExecutionRequest,
)
from orca.operations.manual_step_models import (
    ConfirmManualStepRequest,
    ListPendingManualStepsRequest,
    PendingManualStepDTO,
)
from orca.operations.labware_models import (
    CarryOverrideResponse,
    ClearLabwareCarryOverrideRequest,
    EditLabwareBarcodeRequest,
    SetLabwareCarryOverrideRequest,
    EditLabwareLocationRequest,
    GetLabwareByBarcodeRequest,
    GetLabwareByIdRequest,
    GetLabwareHistoryRequest,
    GetLabwareHistoryResponse,
    GetLabwareResponse,
    ConfirmTipStateRequest,
    ReleaseMoverHoldRequest,
    ConfirmWellVolumesRequest,
    GetTipStateRequest,
    GetTipStateResponse,
    MarkTipsUsedRequest,
    MarkTipsUsedResponse,
    ResolveContentsRequest,
    ResolveContentsResponse,
    GetWellVolumesRequest,
    GetWellVolumesResponse,
    SetTipStateRequest,
    ListLabwareResponse,
    RegisterLabwareRequest,
    RegisterLabwareResponse,
    ResetLabwareLocationRequest,
    SetWellVolumesRequest,
)
from orca.operations.ops_history import SearchOpsHistoryRequest
from orca.operations.submission_models import (
    GetSubmissionRequest,
    GetSubmissionResponse,
    ListSubmissionsRequest,
    ListSubmissionsResponse,
)
from orca.operations.thread_models import (
    AbortMethodRequest,
    InsertActionRequest,
    InsertMethodRequest,
    InsertWhere,
    PauseRequest,
    RecoverThreadRequest,
    ReplaceActionRequest,
    ReplaceMethodRequest,
    ReplaceResult,
    ResumeRequest,
    SkipActionRequest,
    SkipMethodRequest,
    SpawnThreadRequest,
)


_DEFAULT_TIMEOUT_S = 30.0
# Mount and workflow load import user code; the first import on a fresh install
# also compiles it. Devices come up later, on first use, not here.
_IMPORT_TIMEOUT_S = 600.0
_T = TypeVar("_T")


def _coerce_option_value(value: object) -> OptionValueJson:
    """Narrow a Protocol-shaped variable value to `OptionValueJson`.

    The Protocol accepts `Mapping[str, object]` so cloud and local backends
    share the call signature; the daemon Request model uses the narrower
    `OptionValueJson = Union[str, int, float, bool]`. Non-conforming values
    surface here as a TypeError instead of silently corrupting the JSON body.
    """
    if isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"variable values must be str | int | float | bool; got {type(value).__name__}",
    )


def _mode_body(mode: WorkflowRunMode | None) -> dict[str, str] | None:
    """Lifecycle-route body: only sent when the caller states a world."""
    return {"mode": mode.value} if mode is not None else None


class LocalDaemonClient:
    """HTTP client keyed off the PID file.

    Not a long-lived object -- instantiate per CLI command invocation.
    Construction fails fast (via `output.fail`) if no live daemon is
    reachable, so command bodies can treat `LocalDaemonClient()` as a
    precondition and skip a separate guard.
    """

    def __init__(
        self,
        pid_file_path: Path | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if pid_file_path is None:
            pid_file_path = _default_pid_file_path()
        info = detect_live_daemon(pid_file_path)
        if info is None:
            output.fail(
                "no daemon running (run `orca start` first)",
                code=output.EXIT_NOT_CONNECTED,
            )
        self._info: DaemonInfo = info
        self._base_url = f"http://127.0.0.1:{info.port}"
        self._timeout = timeout

    @property
    def info(self) -> DaemonInfo:
        return self._info

    # -- HTTP primitives with error mapping -----------------------------------

    @contextmanager
    def _client(
        self, *, timeout_s: float | None = None, map_transport_errors: bool = True,
        waits_on_hardware: bool = False,
    ) -> Iterator[httpx.Client]:
        """Yield a client, turning a transport failure into a typed exit.

        The yield is inside the `try`, so this also catches errors raised by
        the caller's request, which is every verb in this file. `health()`
        opts out: a dead daemon is one of its valid answers.

        `waits_on_hardware` drops the read limit. The daemon times each device
        command from the driver's advertised duration, and a shake can run for hours.
        """
        limit = self._timeout if timeout_s is None else timeout_s
        try:
            with self._make_http_client() as c:
                if timeout_s is not None or waits_on_hardware:
                    c.timeout = httpx.Timeout(limit, read=None if waits_on_hardware else limit)
                yield c
        except httpx.HTTPError as e:
            if not map_transport_errors:
                raise
            message, code = output.transport_failure(
                e, f"the daemon at {self._base_url}", limit,
            )
            output.fail(message, code=code)

    def _make_http_client(self) -> httpx.Client:
        """Build the underlying `httpx.Client`. Override-point for tests.

        Tests inject `httpx.MockTransport` by subclassing or monkey-patching
        this method, keeping the rest of the public API untouched.
        """
        return httpx.Client(base_url=self._base_url, timeout=self._timeout)

    def _check(self, resp: httpx.Response) -> None:
        """Non-2xx -> typed exit via output.fail(). Never returns on error.

        Renders the typed envelope shape (``{detail: {code, message,
        extras}}``) as ``error: <code>: <message>``, matching the
        cloud-client wire formatter. Falls back to the bare
        ``{detail: str}`` shape and finally to the raw response body
        when neither parses. The HTTP-layer prefix
        (``daemon call failed (N):``) is intentionally dropped --
        operators reading CLI output want the typed message, not the
        transport mechanics.
        """
        if 200 <= resp.status_code < 300:
            return
        message: str | None = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            raw_detail = payload.get("detail")
            if isinstance(raw_detail, dict):
                code = raw_detail.get("code")
                msg = raw_detail.get("message")
                if isinstance(code, str) and isinstance(msg, str):
                    message = f"{code}: {msg}"
                elif isinstance(msg, str):
                    message = msg
                elif isinstance(code, str):
                    # Typed code without a message -- surface the code so
                    # operators can grep / script against it; better than
                    # raw-JSON fallback.
                    message = code
            elif isinstance(raw_detail, str):
                message = raw_detail
        if message is None:
            message = resp.text or f"HTTP {resp.status_code}"
        output.fail(message, code=output.exit_code_for_status(resp.status_code))

    # -- Health ---------------------------------------------------------------

    def health(self) -> bool:
        """Liveness check against the loopback daemon.

        Returns True on any 2xx; False otherwise. Does NOT raise on connection
        failure -- a freshly-started daemon may not have bound the port yet.
        """
        try:
            with self._client(map_transport_errors=False) as c:
                resp = c.get("/health")
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300

    # -- Topology mount + workflow load ---------------------------------------

    def mount_topology(self, spec: str, sim: bool = False) -> RuntimeState:
        """POST /mount-topology. Builds + starts an empty runtime from a
        ``build_topology(stores)`` factory spec. Returns the post-mount state."""
        body = MountTopologyRequest(spec=spec, sim=sim)
        with self._client(timeout_s=_IMPORT_TIMEOUT_S) as c:
            resp = c.post("/mount-topology", json=body.model_dump(mode="json"))
        self._check(resp)
        return MountTopologyResponse.model_validate(resp.json()).runtime_state

    def load_workflow(self, spec: str) -> str:
        """POST /workflows. Registers a workflow from a ``build_workflow(topology)``
        factory spec against the mounted topology. Returns the workflow name."""
        body = RegisterWorkflowRequest(spec=spec)
        with self._client(timeout_s=_IMPORT_TIMEOUT_S) as c:
            resp = c.post("/workflows", json=body.model_dump(mode="json"))
        self._check(resp)
        return RegisterWorkflowResponse.model_validate(resp.json()).workflow_name

    # -- Executions -----------------------------------------------------------

    def submit_workflow(
        self, workflow_name: str,
        variables: Mapping[str, object] | None = None,
        *,
        run_mode: RunModeStr,
        profile_path: str | None = None,
        acknowledge_warnings: bool = False,
    ) -> ControlPlaneExecutionRecordDTO:
        """Submit a workflow.

        `profile_path` is daemon-only -- the loopback daemon and CLI share a
        filesystem, so the daemon reads the JSON profile by absolute path. This
        kwarg is accepted and ignored on a cloud backend; the dispatcher
        guards `--profile` at the CLI verb layer.

        `acknowledge_warnings` maps to the
        `LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED` gate: True bypasses
        the gate, False (default) lets the runtime refuse the submission with
        the override list so the operator can review.
        """
        # Pydantic does the runtime narrowing of `object` -> OptionValueJson; the
        # annotation gap (Mapping[str, object] vs dict[str, OptionValueJson]) is
        # an unavoidable consequence of the Protocol staying CLI-shaped while the
        # daemon Request model uses the narrower union.
        coerced_vars: dict[str, OptionValueJson] | None = (
            {k: _coerce_option_value(v) for k, v in variables.items()}
            if variables is not None else None
        )
        request = SubmitWorkflowRequest(
            workflow_name=workflow_name,
            variables=coerced_vars,
            profile_path=profile_path,
            run_mode=run_mode,
            acknowledge_warnings=acknowledge_warnings,
        )
        with self._client() as c:
            resp = c.post("/executions", json=request.model_dump(exclude_none=True))
        self._check(resp)
        return ControlPlaneExecutionRecordDTO.model_validate(resp.json())

    def submit_method(
        self,
        workflow_name: str,
        method_name: str,
        labware_start: Mapping[str, str],
        labware_end: Mapping[str, str],
        variables: Mapping[str, object] | None = None,
        *,
        run_mode: RunModeStr,
        acknowledge_warnings: bool = False,
    ) -> ControlPlaneExecutionRecordDTO:
        """Submit a standalone method execution.

        Maps to the daemon's POST /method-executions which calls
        runtime.submit_method (synthesizes a one-method workflow,
        registers via the standard execution machinery). Both backends
        support this since the SDK's StandaloneMethodExecutor is
        generic.
        """
        coerced_vars: dict[str, OptionValueJson] | None = (
            {k: _coerce_option_value(v) for k, v in variables.items()}
            if variables is not None else None
        )
        body = SubmitMethodExecutionRequest(
            workflow_name=workflow_name,
            method_name=method_name,
            labware_start=dict(labware_start),
            labware_end=dict(labware_end),
            variables=coerced_vars,
            run_mode=run_mode,
            acknowledge_warnings=acknowledge_warnings,
        )
        with self._client() as c:
            resp = c.post(
                "/method-executions",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return ControlPlaneExecutionRecordDTO.model_validate(resp.json())

    def list_executions(self) -> Sequence[ControlPlaneExecutionRecordDTO]:
        with self._client() as c:
            resp = c.get("/operations/list-executions")
        self._check(resp)
        payload = resp.json()
        execs = payload.get("executions", []) if isinstance(payload, dict) else []
        return [ControlPlaneExecutionRecordDTO.model_validate(r) for r in execs]

    def get_execution(self, execution_id: str) -> ControlPlaneExecutionDetailDTO:
        body = GetExecutionDetailRequest(execution_id=execution_id)
        with self._client() as c:
            resp = c.post(
                "/operations/get-execution-detail",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return ControlPlaneExecutionDetailDTO.model_validate(resp.json())

    def stop_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> StopExecutionResultDTO:
        body = StopExecutionRequest(execution_id=execution_id, confirm=confirm)
        with self._client() as c:
            resp = c.post(
                "/operations/stop-execution",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return StopExecutionResultDTO.model_validate(resp.json())

    # -- Threads --------------------------------------------------------------

    def list_threads(self, execution_id: str) -> list[ThreadSnapshotDTO]:
        with self._client() as c:
            resp = c.get(f"/executions/{execution_id}/threads")
        self._check(resp)
        return [ThreadSnapshotDTO.model_validate(t) for t in resp.json()]

    def get_thread_detail(
        self, execution_id: str, thread_id: str,
    ) -> ThreadSnapshotDTO:
        with self._client() as c:
            resp = c.get(f"/executions/{execution_id}/threads/{thread_id}")
        self._check(resp)
        return ThreadSnapshotDTO.model_validate(resp.json())

    def pause_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> None:
        body = PauseRequest(
            scope=ExecutionScope(execution_id=execution_id),
            reason=reason,
        )
        with self._client() as c:
            resp = c.post("/operations/pause", json=body.model_dump(mode="json"))
        self._check(resp)

    def resume_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> ResumeAllResultDTO:
        body = ResumeRequest(
            scope=ExecutionScope(execution_id=execution_id),
            reason=reason,
        )
        with self._client() as c:
            resp = c.post("/operations/resume", json=body.model_dump(mode="json"))
        self._check(resp)
        result = resp.json().get("result", {})
        return ResumeAllResultDTO.model_validate(result)

    def pause_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None:
        body = PauseRequest(
            scope=ThreadScope(execution_id=execution_id, thread_id=thread_id),
            reason=reason,
        )
        with self._client() as c:
            resp = c.post("/operations/pause", json=body.model_dump(mode="json"))
        self._check(resp)

    def resume_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None:
        body = ResumeRequest(
            scope=ThreadScope(execution_id=execution_id, thread_id=thread_id),
            reason=reason,
        )
        with self._client() as c:
            resp = c.post("/operations/resume", json=body.model_dump(mode="json"))
        self._check(resp)

    def recover_thread(
        self, execution_id: str, thread_id: str, decision: str,
    ) -> None:
        body = RecoverThreadRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            decision=decision,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/recover-thread", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    # -- Thread mutation -------------------------------------------------

    def thread_skip_method(
        self, execution_id: str, thread_id: str, *,
        method_name: str,
        reason: str,
    ) -> None:
        body = SkipMethodRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            method_name=method_name,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/skip-method", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def thread_abort_method(
        self, execution_id: str, thread_id: str, *,
        method_name: str,
        reason: str,
    ) -> None:
        body = AbortMethodRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            method_name=method_name,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/abort-method", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def thread_insert_method(
        self, execution_id: str, thread_id: str, *,
        template_name: str | None = None,
        method_code: str | None = None,
        where: InsertWhere,
        anchor: str | None = None,
        reason: str,
    ) -> None:
        body = InsertMethodRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            template_name=template_name,
            method_code=method_code,
            where=where,
            anchor=anchor,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/insert-method", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def thread_skip_action(
        self, execution_id: str, thread_id: str, *,
        action_id: str | None = None,
        action_command: str | None = None,
        reason: str,
    ) -> None:
        body = SkipActionRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            action_id=action_id,
            action_command=action_command,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/skip-action", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def thread_insert_action(
        self, execution_id: str, thread_id: str, *,
        action_code: str,
        where: InsertWhere,
        anchor: str | None = None,
        reason: str,
    ) -> None:
        body = InsertActionRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            action_code=action_code,
            where=where,
            anchor=anchor,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/insert-action", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def thread_replace_method(
        self, execution_id: str, thread_id: str, *,
        target_name: str,
        template_name: str | None = None,
        method_code: str | None = None,
        reason: str,
    ) -> ReplaceResult:
        body = ReplaceMethodRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            target_name=target_name,
            template_name=template_name,
            method_code=method_code,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/replace-method", json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return ReplaceResult.model_validate(resp.json())

    def thread_replace_action(
        self, execution_id: str, thread_id: str, *,
        target_command: str,
        action_code: str,
        reason: str,
    ) -> ReplaceResult:
        body = ReplaceActionRequest(
            execution_id=execution_id,
            thread_id=thread_id,
            target_command=target_command,
            action_code=action_code,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/replace-action", json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return ReplaceResult.model_validate(resp.json())

    def audit_list(
        self, *,
        action_name: str | None = None,
        limit: int = 200,
    ) -> Sequence[ControlPlaneAuditEntryDTO]:
        params: dict[str, str | int] = {"limit": limit}
        if action_name is not None:
            params["action_name"] = action_name
        with self._client() as c:
            resp = c.get("/audit", params=params)
        self._check(resp)
        payload = resp.json()
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        return [ControlPlaneAuditEntryDTO.model_validate(e) for e in entries]

    # -- Variables ------------------------------------------------------------

    def variables_list(self, execution_id: str) -> dict[str, OptionValueJson]:
        with self._client() as c:
            resp = c.get(f"/variables/{execution_id}")
        self._check(resp)
        return VariablesListResponse.model_validate(resp.json()).root

    def variables_get(
        self, execution_id: str, name: str, submission_id: str | None = None,
    ) -> VariableValue:
        params = {} if submission_id is None else {"submission_id": submission_id}
        with self._client() as c:
            resp = c.get(f"/variables/{execution_id}/{name}", params=params)
        self._check(resp)
        return VariableValue.model_validate(resp.json())

    def variables_resolution(
        self, execution_id: str, name: str,
    ) -> VariableResolutionResponse:
        with self._client() as c:
            resp = c.get(f"/variables/{execution_id}/{name}/resolution")
        self._check(resp)
        return VariableResolutionResponse.model_validate(resp.json())

    def variables_set(
        self, execution_id: str, name: str, value: OptionValueJson,
    ) -> VariableSetResponse:
        body = VariableSetRequest(value=value)
        with self._client() as c:
            resp = c.put(
                f"/variables/{execution_id}/{name}",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return VariableSetResponse.model_validate(resp.json())

    def variables_set_submission(
        self, execution_id: str, submission_id: str, name: str,
        value: OptionValueJson,
    ) -> None:
        body = VariableSetRequest(value=value)
        with self._client() as c:
            resp = c.put(
                f"/variables/submissions/{execution_id}/{submission_id}/{name}",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def variables_set_global(self, name: str, value: OptionValueJson) -> None:
        body = VariableSetRequest(value=value)
        with self._client() as c:
            resp = c.put(
                f"/variables/global/{name}",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def variables_unset(self, execution_id: str, name: str) -> None:
        with self._client() as c:
            resp = c.delete(f"/variables/{execution_id}/{name}")
        self._check(resp)

    def variables_unset_submission(
        self, execution_id: str, submission_id: str, name: str,
    ) -> None:
        with self._client() as c:
            resp = c.delete(
                f"/variables/submissions/{execution_id}/{submission_id}/{name}",
            )
        self._check(resp)

    # -- Device detail + Plugin reads ----------------------------------------

    def device_info(self, device_name: str) -> DeviceDTO:
        with self._client() as c:
            resp = c.get(f"/devices/{device_name}")
        self._check(resp)
        return DeviceDTO.model_validate(resp.json())

    def device_registry_list(self) -> list[ControlPlaneDeviceRegistryEntryDTO]:
        """List the unified device registry.

        Mirrors the cloud REST `GET /api/devices/registry` response shape;
        `orca device registry list` renders the same payload regardless
        of backend. Returns the Protocol-shaped DTO so cloud + daemon
        share one rendering path.
        """
        with self._client() as c:
            resp = c.get("/devices/registry")
        self._check(resp)
        # Daemon wraps in {"devices": [...]}; unwrap to the Protocol's
        # flat list shape.
        payload = resp.json()
        return [
            ControlPlaneDeviceRegistryEntryDTO.model_validate(d)
            for d in payload.get("devices", [])
        ]

    def device_registry_show(
        self, device_id: str,
    ) -> ControlPlaneDeviceRegistryEntryDTO:
        """Show one device's registry entry. 404 raises EXIT_NOT_FOUND."""
        with self._client() as c:
            resp = c.get(f"/devices/registry/{device_id}")
        self._check(resp)
        return ControlPlaneDeviceRegistryEntryDTO.model_validate(resp.json())

    def plugins_list(self) -> list[PluginDTO]:
        with self._client() as c:
            resp = c.get("/plugins")
        self._check(resp)
        return [PluginDTO.model_validate(p) for p in resp.json()]

    def plugins_commands(self) -> list[PluginCommandDTO]:
        with self._client() as c:
            resp = c.get("/plugins/commands")
        self._check(resp)
        return [PluginCommandDTO.model_validate(c) for c in resp.json()]

    # -- Labware (read-only) --------------------------------------------------

    def labware_list(self) -> list[LabwareDTO]:
        with self._client() as c:
            resp = c.get("/operations/list-labware")
        self._check(resp)
        snaps = ListLabwareResponse.model_validate(resp.json()).labware
        return [LabwareDTO.model_validate(s.model_dump()) for s in snaps]

    def labware_get_by_id(self, labware_id: str) -> LabwareDTO:
        body = GetLabwareByIdRequest(labware_id=labware_id)
        with self._client() as c:
            resp = c.post(
                "/operations/get-labware-by-id",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        snap = GetLabwareResponse.model_validate(resp.json()).labware
        return LabwareDTO.model_validate(snap.model_dump())

    def labware_get_by_id_or_none(self, labware_id: str) -> LabwareDTO | None:
        """Like :meth:`labware_get_by_id` but returns None on 404 instead
        of exiting. Other errors still bail via :meth:`_check`.

        Lets ``labware where`` try id then barcode without the first miss
        leaking a 404 to stderr (the Bug 3 history).
        """
        body = GetLabwareByIdRequest(labware_id=labware_id)
        with self._client() as c:
            resp = c.post(
                "/operations/get-labware-by-id",
                json=body.model_dump(mode="json"),
            )
        if resp.status_code == 404:
            return None
        self._check(resp)
        snap = GetLabwareResponse.model_validate(resp.json()).labware
        return LabwareDTO.model_validate(snap.model_dump())

    def labware_get_by_barcode(self, barcode: str) -> LabwareDTO:
        body = GetLabwareByBarcodeRequest(barcode=barcode)
        with self._client() as c:
            resp = c.post(
                "/operations/get-labware-by-barcode",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        snap = GetLabwareResponse.model_validate(resp.json()).labware
        return LabwareDTO.model_validate(snap.model_dump())

    def labware_get_by_barcode_or_none(
        self, barcode: str,
    ) -> LabwareDTO | None:
        """Non-bailing variant of :meth:`labware_get_by_barcode`."""
        body = GetLabwareByBarcodeRequest(barcode=barcode)
        with self._client() as c:
            resp = c.post(
                "/operations/get-labware-by-barcode",
                json=body.model_dump(mode="json"),
            )
        if resp.status_code == 404:
            return None
        self._check(resp)
        snap = GetLabwareResponse.model_validate(resp.json()).labware
        return LabwareDTO.model_validate(snap.model_dump())

    def labware_history(self, labware_id: str) -> list[LocationEventDTO]:
        body = GetLabwareHistoryRequest(labware_id=labware_id)
        with self._client() as c:
            resp = c.post(
                "/operations/get-labware-history",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        events = GetLabwareHistoryResponse.model_validate(resp.json()).history
        return [LocationEventDTO.model_validate(e.model_dump()) for e in events]

    # -- Calibration registries (read-only) -----------------------------------

    def access_configs_list(self) -> list[AccessConfigDTO]:
        with self._client() as c:
            resp = c.get("/access-configs")
        self._check(resp)
        return [AccessConfigDTO.model_validate(x) for x in resp.json()]

    def access_configs_get(self, name: str) -> AccessConfigDTO:
        with self._client() as c:
            resp = c.get(f"/access-configs/{name}")
        self._check(resp)
        return AccessConfigDTO.model_validate(resp.json())

    def access_configs_add(self, config: AccessConfigDTO) -> AccessConfigDTO:
        with self._client() as c:
            resp = c.post("/access-configs", json=config.model_dump(mode="json"))
        self._check(resp)
        return AccessConfigDTO.model_validate(resp.json())

    def access_configs_update(self, config: AccessConfigDTO) -> AccessConfigDTO:
        with self._client() as c:
            resp = c.put(
                f"/access-configs/{config.name}",
                json=config.model_dump(mode="json"),
            )
        self._check(resp)
        return AccessConfigDTO.model_validate(resp.json())

    def access_configs_delete(self, name: str) -> None:
        with self._client() as c:
            resp = c.delete(f"/access-configs/{name}")
        self._check(resp)

    def grip_profiles_list(self) -> list[GripProfileDTO]:
        with self._client() as c:
            resp = c.get("/grip-profiles")
        self._check(resp)
        return [GripProfileDTO.model_validate(x) for x in resp.json()]

    def grip_profiles_get(self, labware_type: str) -> GripProfileDTO:
        with self._client() as c:
            resp = c.get(f"/grip-profiles/{labware_type}")
        self._check(resp)
        return GripProfileDTO.model_validate(resp.json())

    def grip_profiles_patch(
        self, labware_type: str, body: GripProfilePatchRequest,
    ) -> GripProfileDTO:
        with self._client() as c:
            resp = c.patch(
                f"/grip-profiles/{labware_type}",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return GripProfileDTO.model_validate(resp.json())

    def grip_profiles_reset(self, labware_type: str) -> None:
        with self._client() as c:
            resp = c.delete(f"/grip-profiles/{labware_type}")
        self._check(resp)

    def move_defaults_list(self) -> list[MoveDefaultsDTO]:
        with self._client() as c:
            resp = c.get("/move-defaults")
        self._check(resp)
        return [MoveDefaultsDTO.model_validate(x) for x in resp.json()]

    def move_defaults_get(self, transporter_name: str) -> MoveDefaultsDTO:
        with self._client() as c:
            resp = c.get(f"/move-defaults/{transporter_name}")
        self._check(resp)
        return MoveDefaultsDTO.model_validate(resp.json())

    def move_defaults_patch(
        self, transporter_name: str, body: MoveDefaultsPatchRequest,
    ) -> MoveDefaultsDTO:
        with self._client() as c:
            resp = c.patch(
                f"/move-defaults/{transporter_name}",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return MoveDefaultsDTO.model_validate(resp.json())

    def move_defaults_reset(self, transporter_name: str) -> None:
        with self._client() as c:
            resp = c.delete(f"/move-defaults/{transporter_name}")
        self._check(resp)

    def teachpoints_list(self, device_id: str) -> list[TeachpointDTO]:
        with self._client() as c:
            resp = c.get(f"/teachpoints/{device_id}")
        self._check(resp)
        return [TeachpointDTO.model_validate(x) for x in resp.json()]

    def teachpoints_list_all(self) -> list[TeachpointDTO]:
        with self._client() as c:
            resp = c.get("/teachpoints")
        self._check(resp)
        return [TeachpointDTO.model_validate(x) for x in resp.json()]

    def teachpoints_get(self, device_id: str, position_id: str) -> TeachpointDTO:
        with self._client() as c:
            resp = c.get(f"/teachpoints/{device_id}/{position_id}")
        self._check(resp)
        return TeachpointDTO.model_validate(resp.json())

    def teachpoints_add(
        self,
        device_id: str,
        position_id: str,
        coord_type: str,
        coords: Mapping[str, float | int | str],
        access_config_name: str | None,
        gateway: str | None,
        orientation: str | None,
        taught_with: str | None = None,
    ) -> TeachpointDTO:
        body = {
            "device_id": device_id,
            "position_id": position_id,
            "coord_type": coord_type,
            "coords": dict(coords),
            "access_config_name": access_config_name,
            "gateway": gateway,
            "orientation": orientation,
            "taught_with": taught_with,
        }
        with self._client() as c:
            resp = c.post("/teachpoints", json=body)
        self._check(resp)
        return TeachpointDTO.model_validate(resp.json())

    def teachpoints_update(
        self,
        device_id: str,
        position_id: str,
        coords: Mapping[str, float | int | str],
        access_config_name: str | None,
        gateway: str | None,
        orientation: str | None,
        update_access_config: bool,
        update_gateway: bool,
        update_orientation: bool,
        taught_with: str | None = None,
        update_taught_with: bool = False,
    ) -> TeachpointDTO:
        body = {
            "coords": dict(coords),
            "access_config_name": access_config_name,
            "gateway": gateway,
            "orientation": orientation,
            "update_access_config": update_access_config,
            "update_gateway": update_gateway,
            "update_orientation": update_orientation,
            "taught_with": taught_with,
            "update_taught_with": update_taught_with,
        }
        with self._client() as c:
            resp = c.put(f"/teachpoints/{device_id}/{position_id}", json=body)
        self._check(resp)
        return TeachpointDTO.model_validate(resp.json())

    def teachpoints_set_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        body: GripProfilePatchRequest,
    ) -> TeachpointDTO:
        with self._client() as c:
            resp = c.patch(
                f"/teachpoints/{device_id}/{position_id}"
                f"/labware/{labware_type}",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return TeachpointDTO.model_validate(resp.json())

    def teachpoints_clear_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
    ) -> None:
        with self._client() as c:
            resp = c.delete(
                f"/teachpoints/{device_id}/{position_id}"
                f"/labware/{labware_type}",
            )
        self._check(resp)

    def teachpoints_delete(self, device_id: str, position_id: str) -> None:
        with self._client() as c:
            resp = c.delete(f"/teachpoints/{device_id}/{position_id}")
        self._check(resp)

    def deck_layouts_list(
        self, device_id: str,
    ) -> list[DeckLayoutSummaryDTO]:
        with self._client() as c:
            resp = c.get(f"/deck-layouts/{device_id}")
        self._check(resp)
        return [DeckLayoutSummaryDTO.model_validate(x) for x in resp.json()]

    def deck_layouts_list_all(self) -> list[DeckLayoutSummaryDTO]:
        with self._client() as c:
            resp = c.get("/deck-layouts")
        self._check(resp)
        return [DeckLayoutSummaryDTO.model_validate(x) for x in resp.json()]

    def list_reservations(
        self, execution_id: str,
    ) -> list[ReservationSnapshotDTO]:
        """Per-execution reservation listing.
        """
        with self._client() as c:
            resp = c.get(f"/executions/{execution_id}/reservations")
        self._check(resp)
        return [ReservationSnapshotDTO.model_validate(x) for x in resp.json()]

    def reservations_list_all(self) -> list[ReservationSnapshotDTO]:
        """Cross-execution reservation listing.

        Each row carries ``execution_id`` populated so callers can
        re-group client-side. The per-execution counterpart leaves
        ``execution_id`` null since the URL already carries it.
        """
        with self._client() as c:
            resp = c.get("/reservations")
        self._check(resp)
        return [ReservationSnapshotDTO.model_validate(x) for x in resp.json()]

    def manual_steps_list(
        self, execution_id: str | None,
    ) -> list[PendingManualStepDTO]:
        """List emitted-but-unconfirmed operator manual steps.

        ``execution_id=None`` spans all executions; a value scopes to one.
        """
        body = ListPendingManualStepsRequest(execution_id=execution_id)
        with self._client() as c:
            resp = c.post(
                "/operations/list-pending-manual-steps",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        pending = resp.json().get("pending", [])
        return [PendingManualStepDTO.model_validate(x) for x in pending]

    def manual_step_confirm(self, execution_id: str, step_id: str) -> None:
        """Confirm one pending operator manual step (SAFE; fires immediately)."""
        body = ConfirmManualStepRequest(
            execution_id=execution_id, step_id=step_id,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/confirm-manual-step",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def variables_list_all(self) -> list[CrossExecVariableDTO]:
        """Cross-execution variable listing."""
        with self._client() as c:
            resp = c.get("/variables")
        self._check(resp)
        return [CrossExecVariableDTO.model_validate(x) for x in resp.json()]

    def deck_layouts_get(
        self, device_id: str, name: str,
    ) -> DeckLayoutDTO:
        with self._client() as c:
            resp = c.get(f"/deck-layouts/{device_id}/{name}")
        self._check(resp)
        return DeckLayoutDTO.model_validate(resp.json())

    def deck_layouts_add(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> DeckLayoutDTO:
        with self._client() as c:
            resp = c.post(
                f"/deck-layouts/{device_id}/{name}",
                json=config.model_dump(mode="json"),
            )
        self._check(resp)
        return DeckLayoutDTO.model_validate(resp.json())

    def deck_layouts_update(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> DeckLayoutDTO:
        with self._client() as c:
            resp = c.put(
                f"/deck-layouts/{device_id}/{name}",
                json=config.model_dump(mode="json"),
            )
        self._check(resp)
        return DeckLayoutDTO.model_validate(resp.json())

    def deck_layouts_delete(self, device_id: str, name: str) -> None:
        with self._client() as c:
            resp = c.delete(f"/deck-layouts/{device_id}/{name}")
        self._check(resp)

    def labware_edit_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None:
        body = EditLabwareLocationRequest(
            labware_id=labware_id, location=location, reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/edit-labware-location",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def labware_edit_barcode(self, labware_id: str, barcode: str) -> None:
        body = EditLabwareBarcodeRequest(
            labware_id=labware_id, new_barcode=barcode,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/edit-labware-barcode",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def labware_set_carry_override(
        self, labware_id: str, patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (),
    ) -> MoveParameterPatch:
        body = SetLabwareCarryOverrideRequest(
            labware_id=labware_id, set=patch, clear=list(clear),
        )
        with self._client() as c:
            resp = c.post(
                "/operations/set-labware-carry-override",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return CarryOverrideResponse.model_validate(resp.json()).carry_override

    def labware_clear_carry_override(self, labware_id: str) -> None:
        body = ClearLabwareCarryOverrideRequest(labware_id=labware_id)
        with self._client() as c:
            resp = c.post(
                "/operations/clear-labware-carry-override",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def labware_reset_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None:
        body = ResetLabwareLocationRequest(
            labware_id=labware_id, location=location, reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/reset-labware-location",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def labware_register(
        self, template_name: str | None = None,
        labware_type: str | None = None,
        barcode: str | None = None,
        location: str | None = None,
    ) -> LabwareDTO:
        body = RegisterLabwareRequest(
            template_name=template_name, labware_type=labware_type,
            barcode=barcode, location=location,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/register-labware",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        snap = RegisterLabwareResponse.model_validate(resp.json()).labware
        return LabwareDTO.model_validate(snap.model_dump())

    def labware_get_well_volumes(self, labware_id: str) -> GetWellVolumesResponse:
        body = GetWellVolumesRequest(labware_id=labware_id)
        with self._client() as c:
            resp = c.post(
                "/operations/get-well-volumes",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return GetWellVolumesResponse.model_validate(resp.json())

    def labware_set_well_volumes(
        self, labware_id: str, well_volumes: dict[str, float], reason: str,
    ) -> None:
        body = SetWellVolumesRequest(
            labware_id=labware_id, well_volumes=well_volumes, reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/set-well-volumes",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def labware_resolve_contents(self, labware_id: str) -> ResolveContentsResponse:
        body = ResolveContentsRequest(labware_id=labware_id)
        with self._client() as c:
            resp = c.post(
                "/operations/resolve-labware-contents",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return ResolveContentsResponse.model_validate(resp.json())

    def labware_mark_tips_used(
        self, labware_id: str, positions: list[str], reason: str,
    ) -> MarkTipsUsedResponse:
        body = MarkTipsUsedRequest(
            labware_id=labware_id, positions=positions, reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/mark-tips-used",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return MarkTipsUsedResponse.model_validate(resp.json())

    def labware_get_tip_state(self, labware_id: str) -> GetTipStateResponse:
        body = GetTipStateRequest(labware_id=labware_id)
        with self._client() as c:
            resp = c.post(
                "/operations/get-tip-state",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return GetTipStateResponse.model_validate(resp.json())

    def labware_set_tip_state(
        self, labware_id: str, tip_positions_present: list[str], reason: str,
    ) -> None:
        body = SetTipStateRequest(
            labware_id=labware_id,
            tip_positions_present=tip_positions_present,
            reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/set-tip-state",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def labware_confirm_tip_state(
        self, labware_id: str, reason: str | None = None,
    ) -> None:
        body = ConfirmTipStateRequest(labware_id=labware_id, reason=reason)
        with self._client() as c:
            resp = c.post(
                "/operations/confirm-tip-state",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def release_mover_hold(
        self, mover_name: str, reason: str,
        to_location: str | None = None, force: bool = False,
    ) -> MoverHoldReleaseDTO:
        body = ReleaseMoverHoldRequest(
            mover_name=mover_name, to_location=to_location,
            force=force, reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/release-mover-hold",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return MoverHoldReleaseDTO.model_validate(resp.json())

    def labware_confirm_well_volumes(
        self, labware_id: str, reason: str | None = None,
    ) -> None:
        body = ConfirmWellVolumesRequest(labware_id=labware_id, reason=reason)
        with self._client() as c:
            resp = c.post(
                "/operations/confirm-well-volumes",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    # -- Labware catalog (operator CRUD via deployment_registries) ------------

    def get_labware(self, labware_type: str) -> LabwareCatalogEntryDTO:
        with self._client() as c:
            resp = c.get(f"/labware/{labware_type}")
        self._check(resp)
        return LabwareCatalogEntryDTO.model_validate(resp.json())

    def add_labware(
        self,
        *,
        labware_type: str,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntryDTO:
        body = LabwareCatalogCreateRequest(
            labware_type=labware_type,
            display_name=display_name,
            category=category,
            vendor=vendor,
            plr_class_name=plr_class_name,
            geometry=geometry,
        )
        with self._client() as c:
            resp = c.post("/labware", json=body.model_dump(mode="json"))
        self._check(resp)
        return LabwareCatalogEntryDTO.model_validate(resp.json())

    def update_labware(
        self,
        labware_type: str,
        *,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntryDTO:
        body = LabwareCatalogUpdateRequest(
            display_name=display_name,
            category=category,
            vendor=vendor,
            plr_class_name=plr_class_name,
            geometry=geometry,
        )
        with self._client() as c:
            resp = c.put(
                f"/labware/{labware_type}", json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return LabwareCatalogEntryDTO.model_validate(resp.json())

    def delete_labware(self, labware_type: str) -> None:
        with self._client() as c:
            resp = c.delete(f"/labware/{labware_type}")
        self._check(resp)

    def list_labware(
        self, category: str | None = None,
    ) -> list[LabwareCatalogSummaryDTO]:
        params: dict[str, str] = {}
        if category is not None:
            params["category"] = category
        with self._client() as c:
            resp = c.get("/labware", params=params)
        self._check(resp)
        return list(LabwareCatalogResponseDTO.model_validate(resp.json()).labware)

    # -- Device writes --------------------------------------------------------

    def device_capabilities(self, device_name: str) -> list[CommandDescriptorDTO]:
        with self._client() as c:
            resp = c.get(f"/devices/{device_name}/capabilities")
        self._check(resp)
        return [CommandDescriptorDTO.model_validate(x) for x in resp.json()]

    def device_execute(
        self, device_name: str, command: str,
        options: dict[str, JsonValue] | None = None,
        *, mode: WorkflowRunMode | None = None, confirm: bool = False,
    ) -> DeviceInvocationResultDTO:
        body = DeviceExecuteRequest(
            command=command, options=options, mode=mode, confirm=confirm,
        )
        with self._client(waits_on_hardware=True) as c:
            resp = c.post(
                f"/devices/{device_name}/execute",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return DeviceInvocationResultDTO.model_validate(resp.json())

    def device_invoke(
        self, device_name: str, capability: str,
        kwargs: dict[str, JsonValue] | None = None,
        *, mode: WorkflowRunMode | None = None, confirm: bool = False,
    ) -> DeviceInvocationResultDTO:
        body = DeviceInvokeRequest(
            capability=capability, kwargs=kwargs, mode=mode, confirm=confirm,
        )
        with self._client(waits_on_hardware=True) as c:
            resp = c.post(
                f"/devices/{device_name}/invoke",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return DeviceInvocationResultDTO.model_validate(resp.json())

    def device_initialize(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> None:
        with self._client(waits_on_hardware=True) as c:
            resp = c.post(
                f"/devices/{device_name}/initialize", json=_mode_body(mode),
            )
        self._check(resp)

    def device_connect(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> None:
        with self._client(waits_on_hardware=True) as c:
            resp = c.post(
                f"/devices/{device_name}/connect", json=_mode_body(mode),
            )
        self._check(resp)

    def device_disconnect(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> None:
        with self._client(waits_on_hardware=True) as c:
            resp = c.post(
                f"/devices/{device_name}/disconnect", json=_mode_body(mode),
            )
        self._check(resp)

    def device_reconcile_deck(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> ReconcileDeckResponse:
        body = ReconcileDeckRequest(device_name=device_name, mode=mode)
        with self._client(waits_on_hardware=True) as c:
            resp = c.post(
                "/operations/reconcile-deck",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return ReconcileDeckResponse.model_validate(resp.json())

    def state_unsettled(self) -> UnsettledStateResponse:
        with self._client() as c:
            resp = c.post("/operations/unsettled", json={})
        self._check(resp)
        return UnsettledStateResponse.model_validate(resp.json())

    def device_get_mounted_tips(self, device_name: str) -> GetMountedTipsResponse:
        body = GetMountedTipsRequest(device_name=device_name)
        with self._client() as c:
            resp = c.post(
                "/operations/get-mounted-tips", json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return GetMountedTipsResponse.model_validate(resp.json())

    def device_set_mounted_tips(
        self, device_name: str, mounted: list[MountedTipDTO],
        *, reason: str | None = None,
    ) -> None:
        body = SetMountedTipsRequest(
            device_name=device_name, mounted=mounted, reason=reason,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/set-mounted-tips", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def device_confirm_mounted_tips(
        self, device_name: str, *, reason: str | None = None,
    ) -> None:
        body = ConfirmMountedTipsRequest(device_name=device_name, reason=reason)
        with self._client() as c:
            resp = c.post(
                "/operations/confirm-mounted-tips", json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def device_compare_deck(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> CompareDeckResponse:
        body = CompareDeckRequest(device_name=device_name, mode=mode)
        with self._client(waits_on_hardware=True) as c:
            resp = c.post(
                "/operations/compare-deck",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return CompareDeckResponse.model_validate(resp.json())

    def device_take_control(
        self, device_name: str, *, reason: str | None = None,
    ) -> None:
        body = TakeDeviceControlRequest(device_name=device_name, reason=reason)
        with self._client() as c:
            resp = c.post(
                "/operations/take-device-control",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def device_release_control(self, device_name: str) -> None:
        body = ReleaseDeviceControlRequest(device_name=device_name)
        with self._client() as c:
            resp = c.post(
                "/operations/release-device-control",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def device_clear_fault(self, device_name: str) -> ClearDeviceFaultResponse:
        body = ClearDeviceFaultRequest(device_name=device_name)
        with self._client() as c:
            resp = c.post(
                "/operations/clear-device-fault",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return ClearDeviceFaultResponse.model_validate(resp.json())

    # -- Thread spawn --------------------------------------------------------

    def spawn_thread(
        self, execution_id: str, template_name: str,
        labware_id: str | None = None,
    ) -> ThreadSnapshotDTO:
        body = SpawnThreadRequest(
            execution_id=execution_id,
            template_name=template_name,
            labware_id=labware_id,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/spawn-thread",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return ThreadSnapshotDTO.model_validate(resp.json())

    # -- Submissions ---------------------------------------------------------

    def submission_submit(
        self, request: SubmissionSubmitRequest,
    ) -> SubmitExecutionResponse:
        """POST /operations/submit-execution.

        Returns the descriptor the endpoint actually sends. It mirrors
        ``SubmissionDTO`` field for field and adds ``unsettled``, so parsing it
        as the mirror refuses the response outright.
        """
        with self._client() as c:
            resp = c.post(
                "/operations/submit-execution",
                json=request.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return SubmitExecutionResponse.model_validate(resp.json())

    def submissions_list(
        self, execution_id: str | None = None,
    ) -> list[SubmissionDTO]:
        """POST /operations/list-submissions.

        Migrated from the legacy ``GET /submissions``
        URL to the unified Operations surface. The optional
        ``execution_id`` filter is now a body field rather than a query
        param. The Operation returns ``{"submissions": [...]}``; the
        client unwraps it so the CLI sees a flat list as before.
        """
        body = ListSubmissionsRequest(execution_id=execution_id)
        with self._client() as c:
            resp = c.post(
                "/operations/list-submissions",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return list(ListSubmissionsResponse.model_validate(resp.json()).submissions)

    def submission_get(self, submission_id: str) -> SubmissionDTO:
        """POST /operations/get-submission.

        Migrated from the legacy
        ``GET /submissions/{id}`` URL. The wire shape returns
        ``{"submission": {...}}``; unwrap to keep the CLI signature
        flat.
        """
        body = GetSubmissionRequest(submission_id=submission_id)
        with self._client() as c:
            resp = c.post(
                "/operations/get-submission",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return GetSubmissionResponse.model_validate(resp.json()).submission

    def execution_close(self, execution_id: str) -> SubmissionCloseResponse:
        body = CloseExecutionRequest(execution_id=execution_id)
        with self._client() as c:
            resp = c.post(
                "/operations/close-execution",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)
        return SubmissionCloseResponse.model_validate(resp.json())

    # -- Execution remove + Reservation cancel ------------------------------

    def execution_remove(self, execution_id: str) -> None:
        body = RemoveExecutionRequest(execution_id=execution_id)
        with self._client() as c:
            resp = c.post(
                "/operations/remove-execution",
                json=body.model_dump(mode="json"),
            )
        self._check(resp)

    def reservation_cancel(
        self, execution_id: str, reservation_id: str,
        reason: str | None = None,
    ) -> None:
        query = ReservationCancelQuery(reason=reason)
        with self._client() as c:
            resp = c.delete(
                f"/executions/{execution_id}/reservations/{reservation_id}",
                params=query.model_dump(exclude_none=True),
            )
        self._check(resp)

    # -- Incidents ------------------------------------------------------------

    def incidents_list(
        self,
        unacknowledged_only: bool = False,
        category: str | None = None,
        execution_id: str | None = None,
    ) -> list[IncidentDTO]:
        query = IncidentListQuery(
            unacknowledged_only=unacknowledged_only,
            category=category,
            execution_id=execution_id,
        )
        with self._client() as c:
            resp = c.get("/incidents", params=query.model_dump(exclude_none=True))
        self._check(resp)
        return [IncidentDTO.model_validate(i) for i in resp.json()]

    def incidents_get(self, incident_id: str) -> IncidentDTO:
        with self._client() as c:
            resp = c.get(f"/incidents/{incident_id}")
        self._check(resp)
        return IncidentDTO.model_validate(resp.json())

    def incidents_ack(self, incident_id: str) -> IncidentAckResponse:
        with self._client() as c:
            resp = c.post(f"/incidents/{incident_id}/ack")
        self._check(resp)
        return IncidentAckResponse.model_validate(resp.json())

    def incidents_ack_all(
        self, category: str | None = None,
    ) -> IncidentAckResponse:
        query = IncidentAckAllQuery(category=category)
        with self._client() as c:
            resp = c.post("/incidents/ack-all", params=query.model_dump(exclude_none=True))
        self._check(resp)
        return IncidentAckResponse.model_validate(resp.json())

    # -- Recoverable-timeout operator decisions ------------------------------

    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float,
    ) -> RecoverableTimeoutDecisionResponse:
        with self._client() as c:
            resp = c.post(
                f"/incidents/{incident_id}/recoverable_timeout/extend",
                json={"additional_seconds": additional_seconds},
            )
        self._check(resp)
        return RecoverableTimeoutDecisionResponse.model_validate(resp.json())

    def recoverable_timeout_abort(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse:
        with self._client() as c:
            resp = c.post(
                f"/incidents/{incident_id}/recoverable_timeout/abort",
                json={"operator_name": operator_name, "reason": reason},
            )
        self._check(resp)
        return RecoverableTimeoutDecisionResponse.model_validate(resp.json())

    def recoverable_timeout_mark_complete(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse:
        with self._client() as c:
            resp = c.post(
                f"/incidents/{incident_id}/recoverable_timeout/mark_complete",
                json={"operator_name": operator_name, "reason": reason},
            )
        self._check(resp)
        return RecoverableTimeoutDecisionResponse.model_validate(resp.json())

    # -- Registry (read-only) -------------------------------------------------

    def system_info(self) -> SystemInfoDTO:
        with self._client() as c:
            resp = c.get("/system")
        self._check(resp)
        return SystemInfoDTO.model_validate(resp.json())

    def list_workflows(self) -> list[WorkflowTemplateDTO]:
        with self._client() as c:
            resp = c.get("/catalog/workflows")
        self._check(resp)
        return [WorkflowTemplateDTO.model_validate(x) for x in resp.json()]

    def methods_list(self) -> list[MethodSummaryDTO]:
        """GET /operations/list-methods. Registry-sourced summaries.

        Mirrors the cloud backend's ``methods_list``: parses into the
        Protocol's ``MethodSummaryDTO`` (``failure_policy`` as its name
        string) so both backends render the same shape.
        """
        with self._client() as c:
            resp = c.get("/operations/list-methods")
        self._check(resp)
        entries = resp.json().get("methods", [])
        return [MethodSummaryDTO.model_validate(e) for e in entries]

    def method_get(
        self, name: str, workflow_name: str | None = None,
    ) -> MethodSummaryDTO:
        """POST /operations/get-method. Registry snapshot only.

        ``workflow_name`` disambiguates when the same method name exists in
        more than one workflow. Method source-code reads are a cloud-only
        worktree concern.
        """
        body: dict[str, str] = {"name": name}
        if workflow_name is not None:
            body["workflow_name"] = workflow_name
        with self._client() as c:
            resp = c.post("/operations/get-method", json=body)
        self._check(resp)
        payload = resp.json().get("method", resp.json())
        return MethodSummaryDTO.model_validate(payload)

    def workflow_get(self, name: str) -> WorkflowSummaryDTO:
        """POST /operations/get-workflow. Registry snapshot only."""
        with self._client() as c:
            resp = c.post("/operations/get-workflow", json={"name": name})
        self._check(resp)
        payload = resp.json().get("workflow", resp.json())
        return WorkflowSummaryDTO.model_validate(payload)

    def topology_get(
        self, source: bool = False,
    ) -> TopologyViewDTO | TopologySourceDTO:
        """GET /topology. With ``?source=true`` the daemon returns the factory
        spec it was mounted from (the local source-of-truth); otherwise the
        live composed-topology snapshot.
        """
        params: dict[str, str] = {}
        if source:
            params["source"] = "true"
        with self._client() as c:
            resp = c.get("/topology", params=params)
        self._check(resp)
        body = resp.json()
        if source:
            return TopologySourceDTO.model_validate(body)
        return TopologyViewDTO.model_validate(body)

    def runtime_status(self) -> RuntimeStatusResponseDTO:
        """GET /runtime/status. ``built`` reflects whether a SystemRuntime is
        mounted. The daemon has no build-error tracking (no submission
        pipeline), so ``last_build_error`` is always None.
        """
        with self._client() as c:
            resp = c.get("/runtime/status")
        self._check(resp)
        return RuntimeStatusResponseDTO.model_validate(resp.json())

    def ops_history_get(self, execution_id: str) -> OpsHistoryGetResponseDTO:
        """POST /operations/list-ops-history. Every TrackingRecord archived
        for one execution."""
        with self._client() as c:
            resp = c.post(
                "/operations/list-ops-history",
                json={"execution_id": execution_id},
            )
        self._check(resp)
        return OpsHistoryGetResponseDTO.model_validate(resp.json())

    def ops_history_search(
        self,
        execution_id: str | None = None,
        action_id: str | None = None,
        thread_id: str | None = None,
        method_id: str | None = None,
        source: str | None = None,
        operation: str | None = None,
        labware_name: str | None = None,
        device_name: str | None = None,
    ) -> OpsHistorySearchResponseDTO:
        """POST /operations/search-ops-history. All filters optional;
        AND-combined."""
        body = SearchOpsHistoryRequest(
            execution_id=execution_id,
            action_id=action_id,
            thread_id=thread_id,
            method_id=method_id,
            source=source,
            operation=operation,
            labware_name=labware_name,
            device_name=device_name,
        )
        with self._client() as c:
            resp = c.post(
                "/operations/search-ops-history",
                json=body.model_dump(mode="json", exclude_none=True),
            )
        self._check(resp)
        return OpsHistorySearchResponseDTO.model_validate(resp.json())

    def labware_journey(
        self, labware_id: str, kinds: Sequence[str] | None = None,
    ) -> LabwareJourneyResponseDTO:
        """POST /operations/get-labware-journey. ``kinds`` lists the entry
        kinds to include (``move`` / ``action``); omit to include both."""
        body: dict[str, JsonValue] = {"labware_id": labware_id}
        if kinds is not None:
            body["kinds"] = list(kinds)
        with self._client() as c:
            resp = c.post("/operations/get-labware-journey", json=body)
        self._check(resp)
        return LabwareJourneyResponseDTO.model_validate(resp.json())

    # -- Labware runtime mutations (clear / discharge) -----------------------

    def labware_clear_submission(
        self, submission_id: str, force: bool = False,
    ) -> dict[str, list[str]]:
        """POST /labware/runtime/clear-submission. Clears non-reuse-bound
        labware from the named submission; returns both ``cleared`` and
        ``preserved_reuse_bound`` ids."""
        with self._client() as c:
            resp = c.post(
                "/labware/runtime/clear-submission",
                params={"submission_id": submission_id},
                json={"force": force},
            )
        self._check(resp)
        response = LabwareClearSubmissionResponseDTO.model_validate(resp.json())
        return {
            "cleared": list(response.cleared),
            "preserved_reuse_bound": list(response.preserved_reuse_bound),
        }

    def labware_discharge(
        self, labware_id: str, force: bool = False,
    ) -> dict[str, list[str]]:
        """POST /labware/runtime/discharge. Removes ONE labware instance from
        every runtime store."""
        with self._client() as c:
            resp = c.post(
                "/labware/runtime/discharge",
                params={"labware_id": labware_id},
                json={"force": force},
            )
        self._check(resp)
        response = LabwareClearResponseDTO.model_validate(resp.json())
        return {"cleared_labware_ids": list(response.cleared_labware_ids)}

    def labware_clear_all(
        self, force: bool = False,
    ) -> dict[str, list[str]]:
        """POST /labware/runtime/clear-all. Panic button: clears every labware
        in the runtime."""
        with self._client() as c:
            resp = c.post(
                "/labware/runtime/clear-all",
                json={"force": force},
            )
        self._check(resp)
        response = LabwareClearResponseDTO.model_validate(resp.json())
        return {"cleared_labware_ids": list(response.cleared_labware_ids)}

    def list_thread_templates(self) -> list[ThreadTemplateDTO]:
        with self._client() as c:
            resp = c.get("/catalog/threads")
        self._check(resp)
        return [ThreadTemplateDTO.model_validate(x) for x in resp.json()]

    def list_locations(self) -> list[LocationDTO]:
        with self._client() as c:
            resp = c.get("/catalog/locations")
        self._check(resp)
        return [LocationDTO.model_validate(x) for x in resp.json()]

    def list_devices(self) -> Sequence[ControlPlaneDeviceDTO]:
        """Protocol-shaped listing (name + type_name only).

        Cloud-friendly subset; `orca device list` on the local backend calls
        `list_device_snapshots()` instead to render rich runtime fields.
        """
        with self._client() as c:
            resp = c.get("/catalog/devices")
        self._check(resp)
        return [ControlPlaneDeviceDTO.model_validate(x) for x in resp.json()]

    def list_device_snapshots(self) -> list[DeviceDTO]:
        """Local-only rich listing (runtime state + locations + loaded labware)."""
        with self._client() as c:
            resp = c.get("/catalog/devices")
        self._check(resp)
        return [DeviceDTO.model_validate(x) for x in resp.json()]

    def device_introspection(
        self, device_id: str,
    ) -> ControlPlaneDeviceIntrospectionDTO:
        """Driver introspection (interfaces / capabilities / methods).

        Hits the daemon's GET /devices/{name}/introspection. Same payload shape
        as a hosted deployment's GET /api/devices/{id}/capabilities, so `orca
        device capabilities <id>` renders identically against either backend.
        """
        with self._client() as c:
            resp = c.get(f"/devices/{device_id}/introspection")
        self._check(resp)
        return ControlPlaneDeviceIntrospectionDTO.model_validate(resp.json())
