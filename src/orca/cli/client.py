"""The one HTTP client behind every `orca` verb that reaches a server."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from http import HTTPStatus
from typing import TypeVar

import httpx
from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from pydantic import BaseModel, JsonValue, ValidationError

from orca.cli import output
from orca.cli.control_plane import ENV_CLOUD_API_KEY, ControlPlaneError
from orca.modes.device_modes import DeviceMode
from orca.operations.thread_models import InsertWhere
from orca.variables.errors import OptionValue
from orca.operations.access_config_models import (
    CREATE_ACCESS_CONFIG_ROUTE,
    CreateAccessConfigRequest,
    DELETE_ACCESS_CONFIG_ROUTE,
    DeleteAccessConfigRequest,
    GET_ACCESS_CONFIG_ROUTE,
    GetAccessConfigRequest,
    LIST_ACCESS_CONFIGS_ROUTE,
    UPDATE_ACCESS_CONFIG_ROUTE,
    UpdateAccessConfigRequest,
)
from orca.operations.audit import LIST_AUDIT_ROUTE, ListAuditRequest
from orca.operations.binding import RestBinding, Route
from orca.operations.blocker_models import BlockersPayload
from orca.operations.catalog_models import (
    EmptyRequest,
    GET_METHOD_ROUTE,
    GET_WORKFLOW_ROUTE,
    GetMethodRequest,
    GetWorkflowRequest,
    LIST_METHODS_ROUTE,
    LIST_THREAD_TEMPLATES_ROUTE,
    LIST_WORKFLOWS_ROUTE,
)
from orca.operations.check_run_models import (
    GET_CHECK_RUN_LABWARE_ROUTE,
    GetCheckRunLabwareRequest,
    GetCheckRunLabwareResponse,
)
from orca.operations.deck_layout_models import (
    DELETE_DECK_LAYOUT_ROUTE,
    DeckLayoutResponse,
    DeleteDeckLayoutRequest,
    GET_DECK_LAYOUT_ROUTE,
    GetDeckLayoutRequest,
    LIST_DECK_LAYOUTS_ROUTE,
    ListDeckLayoutsRequest,
    UPDATE_DECK_LAYOUT_ROUTE,
    UpdateDeckLayoutRequest,
)
from orca.operations.device_models import (
    CLEAR_DEVICE_FAULT_ROUTE,
    COMPARE_DECK_ROUTE,
    CONFIRM_MOUNTED_TIPS_ROUTE,
    CONNECT_DEVICE_ROUTE,
    ClearDeviceFaultRequest,
    ClearDeviceFaultResponse,
    CompareDeckRequest,
    CompareDeckResponse,
    ConfirmMountedTipsRequest,
    ConnectDeviceRequest,
    DISCONNECT_DEVICE_ROUTE,
    DisconnectDeviceRequest,
    GET_DEVICE_INTROSPECTION_ROUTE,
    GET_MOUNTED_TIPS_ROUTE,
    GetDeviceIntrospectionRequest,
    GetMountedTipsRequest,
    GetMountedTipsResponse,
    INITIALIZE_DEVICE_ROUTE,
    InitializeDeviceRequest,
    MountedTipDTO,
    RECONCILE_DECK_ROUTE,
    RELEASE_DEVICE_CONTROL_ROUTE,
    ReconcileDeckRequest,
    ReconcileDeckResponse,
    ReleaseDeviceControlRequest,
    SET_DEVICE_MODE_ROUTE,
    SET_MOUNTED_TIPS_ROUTE,
    SetDeviceModeRequest,
    SetDeviceModeResponse,
    SetMountedTipsRequest,
    TAKE_DEVICE_CONTROL_ROUTE,
    TakeDeviceControlRequest,
)
from orca.operations.device_registry_models import (
    LIST_DEVICE_REGISTRY_ROUTE,
    ListDeviceRegistryRequest,
    SHOW_DEVICE_REGISTRY_ENTRY_ROUTE,
    ShowDeviceRegistryEntryRequest,
)
from orca.operations.device_setup_models import (
    DeviceSetupResponse,
    GET_DEVICE_SETUP_ROUTE,
    GetDeviceSetupRequest,
    PUT_DEVICE_SETUP_ROUTE,
    PutDeviceSetupRequest,
)
from orca.operations.events import LIST_EVENTS_ROUTE, ListEventsRequest, PastEvent
from orca.operations.execution_models import (
    CLOSE_EXECUTION_ROUTE,
    CloseExecutionRequest,
    CloseExecutionResponse,
    ExecutionRecordModel,
    GET_EXECUTION_DETAIL_ROUTE,
    GetExecutionDetailRequest,
    GetExecutionDetailResponse,
    LIST_EXECUTIONS_ROUTE,
    ListExecutionsRequest,
    REMOVE_EXECUTION_ROUTE,
    RemoveExecutionRequest,
    SET_EXECUTION_SIM_SPEEDUP_ROUTE,
    STOP_EXECUTION_ROUTE,
    SetExecutionSimSpeedupRequest,
    SetExecutionSimSpeedupResponse,
    StopExecutionRequest,
    StopExecutionResponse,
)
from orca.operations.grip_profile_models import (
    GET_GRIP_PROFILE_ROUTE,
    GetGripProfileRequest,
    LIST_GRIP_PROFILES_ROUTE,
    PATCH_GRIP_PROFILE_ROUTE,
    PatchGripProfileRequest,
    RESET_GRIP_PROFILE_ROUTE,
    ResetGripProfileRequest,
)
from orca.operations.incident_models import (
    ABORT_RECOVERABLE_TIMEOUT_ROUTE,
    ACKNOWLEDGE_ALL_INCIDENTS_ROUTE,
    ACKNOWLEDGE_INCIDENT_ROUTE,
    AbortRecoverableTimeoutRequest,
    AcknowledgeAllIncidentsRequest,
    AcknowledgeIncidentRequest,
    EXTEND_RECOVERABLE_TIMEOUT_ROUTE,
    ExtendRecoverableTimeoutRequest,
    GET_INCIDENT_ROUTE,
    GetIncidentRequest,
    LIST_INCIDENTS_ROUTE,
    ListIncidentsRequest,
    MARK_RECOVERABLE_TIMEOUT_COMPLETE_ROUTE,
    MarkRecoverableTimeoutCompleteRequest,
)
from orca.operations.interventions import (
    NEXT_INTERVENTION_ROUTE,
    NextInterventionRequest,
    NextInterventionResponse,
)
from orca.operations.labware_catalog_models import (
    ADD_LABWARE_ENTRY_ROUTE,
    DELETE_LABWARE_ENTRY_ROUTE,
    GET_LABWARE_CATALOG_ROUTE,
    GET_LABWARE_ENTRY_ROUTE,
    LabwareCatalogEntry,
    LabwareCatalogRequest,
    LabwareCatalogSummary,
    LabwareCreateRequest,
    LabwareEntryRequest,
    LabwareUpdateRequest,
    UPDATE_LABWARE_ENTRY_ROUTE,
)
from orca.operations.labware_models import (
    CLEAR_ALL_LABWARE_ROUTE,
    CLEAR_LABWARE_CARRY_OVERRIDE_ROUTE,
    CLEAR_SUBMISSION_LABWARE_ROUTE,
    CONFIRM_TIP_STATE_ROUTE,
    CONFIRM_WELL_VOLUMES_ROUTE,
    ClearAllLabwareRequest,
    ClearLabwareCarryOverrideRequest,
    ClearSubmissionLabwareRequest,
    ConfirmTipStateRequest,
    ConfirmWellVolumesRequest,
    DISCHARGE_LABWARE_ROUTE,
    DischargeLabwareRequest,
    EDIT_LABWARE_BARCODE_ROUTE,
    EDIT_LABWARE_LOCATION_ROUTE,
    EditLabwareBarcodeRequest,
    EditLabwareLocationRequest,
    GET_LABWARE_BY_BARCODE_ROUTE,
    GET_LABWARE_BY_ID_ROUTE,
    GET_LABWARE_JOURNEY_ROUTE,
    GetLabwareByBarcodeRequest,
    GetLabwareByIdRequest,
    GetLabwareJourneyRequest,
    GetLabwareJourneyResponse,
    LIST_LABWARE_ROUTE,
    LabwareSnapshotModel,
    ListLabwareRequest,
    MARK_TIPS_USED_ROUTE,
    MarkTipsUsedRequest,
    MarkTipsUsedResponse,
    REGISTER_LABWARE_ROUTE,
    RELEASE_MOVER_HOLD_ROUTE,
    RESET_LABWARE_LOCATION_ROUTE,
    RESOLVE_CONTENTS_ROUTE,
    RegisterLabwareRequest,
    ReleaseMoverHoldRequest,
    ReleaseMoverHoldResponse,
    ResetLabwareLocationRequest,
    ResolveContentsRequest,
    ResolveContentsResponse,
    SET_LABWARE_CARRY_OVERRIDE_ROUTE,
    SET_TIP_STATE_ROUTE,
    SET_WELL_VOLUMES_ROUTE,
    SetLabwareCarryOverrideRequest,
    SetTipStateRequest,
    SetWellVolumesRequest,
)
from orca.operations.manual_step_models import (
    CONFIRM_MANUAL_STEP_ROUTE,
    ConfirmManualStepRequest,
    LIST_PENDING_MANUAL_STEPS_ROUTE,
    ListPendingManualStepsRequest,
    PendingManualStepDTO,
)
from orca.operations.move_defaults_models import (
    GET_MOVE_DEFAULTS_ROUTE,
    GetMoveDefaultsRequest,
    LIST_MOVE_DEFAULTS_ROUTE,
    PATCH_MOVE_DEFAULTS_ROUTE,
    PatchMoveDefaultsRequest,
    RESET_MOVE_DEFAULTS_ROUTE,
    ResetMoveDefaultsRequest,
)
from orca.operations.operator_device_models import (
    OperatorRoute,
)
from orca.operations.ops_history import (
    SEARCH_OPS_HISTORY_ROUTE,
    SearchOpsHistoryRequest,
    SearchOpsHistoryResponse,
)
from orca.operations.profile_models import (
    CREATE_PROFILE_ROUTE,
    DELETE_PROFILE_ROUTE,
    DeleteProfileRequest,
    GET_PROFILE_ROUTE,
    LIST_PROFILES_ROUTE,
    ProfileNameRequest,
    SaveProfileRequest,
    UPDATE_PROFILE_ROUTE,
)
from orca.operations.run_loading_models import (
    LOAD_RUN_LABWARE_ROUTE,
    LoadRunLabwareRequest,
    LoadRunLabwareResponse,
)
from orca.operations.runtime_read_models import (
    CANCEL_RESERVATION_ROUTE,
    CancelReservationRequest,
    GET_THREAD_ROUTE,
    GetThreadRequest,
    LIST_PLUGINS_ROUTE,
    LIST_RESERVATIONS_ROUTE,
    ListPluginsRequest,
    ListReservationsRequest,
)
from orca.operations.runtime_status import GET_RUNTIME_STATUS_ROUTE, GetRuntimeStatusRequest
from orca.operations.scope import ExecutionScope, ThreadScope
from orca.operations.state_models import (
    UNSETTLED_STATE_ROUTE,
    UnsettledStateRequest,
    UnsettledStateResponse,
)
from orca.operations.submission_models import (
    GET_SUBMISSION_ROUTE,
    GetSubmissionRequest,
    LIST_SUBMISSIONS_ROUTE,
    ListSubmissionsRequest,
    MethodExecutionRequest,
    MethodExecutionResponse,
    SUBMIT_EXECUTION_ROUTE,
    SUBMIT_METHOD_EXECUTION_ROUTE,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)
from orca.operations.system_loading_models import (
    HEALTH_ROUTE,
    RELOAD_RUNTIME_ROUTE,
    SHUTDOWN_ROUTE,
    UNLOAD_ROUTE,
    UnloadRequest,
)
from orca.operations.system_models import (
    GET_SYSTEM_INFO_ROUTE,
    GetSystemInfoRequest,
    GetSystemInfoResponse,
)
from orca.operations.teachpoint_models import (
    CLEAR_LABWARE_OVERRIDE_ROUTE,
    ClearLabwareOverrideRequest,
    DELETE_TEACHPOINT_ROUTE,
    DeleteTeachpointRequest,
    GET_TEACHPOINT_ROUTE,
    GetTeachpointRequest,
    LIST_TEACHPOINTS_ROUTE,
    ListTeachpointsRequest,
    SET_LABWARE_OVERRIDE_ROUTE,
    SetLabwareOverrideRequest,
    UPDATE_TEACHPOINT_ROUTE,
    UpdateTeachpointRequest,
)
from orca.operations.teachpoint_transfer_models import (
    CREATE_TEACHPOINTS_ROUTE,
    CreateTeachpointsRequest,
    CreateTeachpointsResponse,
)
from orca.operations.thread_models import (
    INSERT_ACTION_ROUTE,
    INSERT_METHOD_ROUTE,
    InsertActionRequest,
    InsertMethodRequest,
    PAUSE_ROUTE,
    PauseRequest,
    RECOVER_THREAD_ROUTE,
    REPLACE_ACTION_ROUTE,
    REPLACE_METHOD_ROUTE,
    RESUME_ROUTE,
    RecoverThreadRequest,
    ReplaceActionRequest,
    ReplaceMethodRequest,
    ReplaceResult,
    ResumeExecutionResult,
    ResumeRequest,
    SKIP_ACTION_ROUTE,
    SKIP_METHOD_ROUTE,
    SPAWN_THREAD_ROUTE,
    SkipActionRequest,
    SkipMethodRequest,
    SpawnThreadRequest,
)
from orca.operations.topology_models import GET_TOPOLOGY_ROUTE, GetTopologyRequest
from orca.operations.variable_models import (
    CrossExecVariableDTO,
    GET_VARIABLE_ROUTE,
    GetVariableRequest,
    LIST_ALL_VARIABLES_ROUTE,
    LIST_VARIABLES_ROUTE,
    ListAllVariablesRequest,
    ListVariablesRequest,
    GET_GLOBAL_VARIABLE_ROUTE,
    SET_GLOBAL_VARIABLE_ROUTE,
    UNSET_GLOBAL_VARIABLE_ROUTE,
    GetGlobalVariableRequest,
    UnsetGlobalVariableRequest,
    SET_SUBMISSION_VARIABLE_ROUTE,
    SET_VARIABLE_ROUTE,
    SetGlobalVariableRequest,
    SetSubmissionVariableRequest,
    SetVariableRequest,
    UNSET_SUBMISSION_VARIABLE_ROUTE,
    UNSET_VARIABLE_ROUTE,
    UnsetSubmissionVariableRequest,
    UnsetVariableRequest,
    VariableSetResponse,
    VariableValue,
)
from orca.operations.wire_models import (
    AccessConfigDTO,
    AuditEntryDTO,
    DeckLayoutDTO,
    DeckLayoutSummaryDTO,
    DeviceIntrospectionDTO,
    DeviceRegistryEntryResponse,
    GripProfileDTO,
    HealthResponse,
    IncidentAckResponse,
    IncidentResponse,
    MethodTemplateDTO,
    MoveDefaultsDTO,
    OptionValueJson,
    PluginDTO,
    RecoverableTimeoutDecisionResponse,
    ReloadRequest,
    ReloadResponse,
    ReservationSnapshotDTO,
    RunModeStr,
    RuntimeStatusResponse,
    SubmissionDTO,
    TeachpointDTO,
    LocationDTO,
    ThreadSnapshotResponse,
    ThreadTemplateDTO,
    TopologySourceResponse,
    TopologyViewResponse,
    WorkflowTemplateDTO,
)
from orca.runtime.move_parameter_models import GripProfilePatchRequest, MoveDefaultsPatchRequest
from orca.variables.deployment_profile import DeploymentProfile


def read_error_envelope(
    resp: httpx.Response,
) -> tuple[str, str | None, dict[str, JsonValue] | None]:
    """An error response as ``(message, code, extras)``.

    Both servers send the envelope for every error. A proxy or an unreachable
    server may send something else, so anything that is not the envelope falls
    back to its text.
    """
    try:
        payload = resp.json()
    except ValueError:
        return resp.text or f"HTTP {resp.status_code}", None, None
    if isinstance(payload, dict):
        d = payload.get("detail")
        if isinstance(d, dict):
            message = d.get("message")
            code = d.get("code")
            code_out = code if isinstance(code, str) else None
            extras = d.get("extras")
            extras_out = extras if isinstance(extras, dict) else None
            if isinstance(message, str):
                return message, code_out, extras_out
            if code_out is not None:
                return code_out, code_out, extras_out
            return str(d), None, None
        if d is not None:
            return str(d), None, None
    return str(payload), None, None


_Request = TypeVar("_Request", bound=BaseModel)
_Response = TypeVar("_Response", bound=BaseModel)


def _parse_with_blockers(
    model: type[_Response], resp: httpx.Response,
) -> tuple[_Response, BlockersPayload]:
    """A response the server sends with the blocker list merged in: the
    Operation's own model, and the blockers. Both halves are strict, so a
    missing or renamed field on either side is refused."""
    body = resp.json()
    blockers = BlockersPayload.model_validate(
        {field: body.pop(field) for field in BlockersPayload.model_fields if field in body},
    )
    return model.model_validate(body), blockers


def server_answer_mismatch(exc: ValidationError) -> ControlPlaneError:
    """The error for an answer this CLI's models refuse: the server is not the same release."""
    return ControlPlaneError(
        f"the server's answer does not match this version of orca ({exc.title}: "
        f"{exc.error_count()} field(s)). Use an orca CLI from the same release as the server.",
        exit_code=output.EXIT_GENERIC,
        cause=exc,
    )


def _convert(model: type[_Response], answer: BaseModel) -> _Response:
    try:
        return model.model_validate(answer.model_dump())
    except ValidationError as exc:
        raise server_answer_mismatch(exc) from exc


_DEFAULT_TIMEOUT_S = 30.0


class OrcaClient:
    """One client for every server: built with a URL and an optional API key.

    Every call takes its method and path from the Operation's route, sends the route's Request
    and parses the route's Response.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT_S,
        peer: str | None = None,
    ) -> None:
        if not base_url:
            raise ControlPlaneError("a server URL is required", exit_code=output.EXIT_USAGE)
        self._base_url = base_url.rstrip("/")
        self._peer = peer or f"the server at {self._base_url}"
        self._api_key = api_key
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    @contextmanager
    def _client(self, binding: RestBinding) -> Iterator[httpx.Client]:
        """Yield a client. A request that gets no answer raises `ControlPlaneError`.

        A binding the server never times out gets no read limit either: a device command can
        run for hours, and a long-poll waits as long as its request asks.
        """
        try:
            with self._make_http_client() as c:
                if binding.never_timed_out:
                    c.timeout = httpx.Timeout(self._timeout, read=None)
                yield c
        except httpx.HTTPError as e:
            message, exit_code = output.transport_failure(e, self._peer, self._timeout)
            raise ControlPlaneError(message, cause=e, exit_code=exit_code) from e

    def _make_http_client(self) -> httpx.Client:
        headers = {} if self._api_key is None else {"X-API-Key": self._api_key}
        return httpx.Client(base_url=self._base_url, timeout=self._timeout, headers=headers)

    def _check(self, resp: httpx.Response) -> None:
        """A non-2xx answer raises `ControlPlaneError`."""
        if 200 <= resp.status_code < 300:
            return
        detail, code, extras = read_error_envelope(resp)
        if resp.status_code == 401:
            message = f"the server rejected the API key (set {ENV_CLOUD_API_KEY})"
        elif code and code != detail:
            message = f"{code}: {detail}"
        else:
            message = detail
        raise ControlPlaneError(
            message, http_status=resp.status_code, code=code, extras=extras, detail=detail,
        )

    def _send(
        self,
        binding: RestBinding,
        request: BaseModel,
    ) -> httpx.Response:
        """Send `request` where `binding` says: path fields in the path, the rest as query or JSON body.

        A field with no value is left out. A field with a default is sent, because a nested default
        can be the tag that tells the server which model it is reading.
        """
        url, fields = binding.fill_url(request.model_dump(mode="json", exclude_none=True))
        with self._client(binding) as c:
            if binding.fields_in_query:
                return c.request(binding.method, url, params=fields)
            return c.request(binding.method, url, json=fields)

    def _answer(
        self, binding: RestBinding, resp: httpx.Response, response: type[_Response],
    ) -> _Response:
        """The answer read as `response`, with the blocker list taken off when the route carries one."""
        self._check(resp)
        if binding.status_code == HTTPStatus.NO_CONTENT:
            return response()
        try:
            if binding.carries_blockers:
                return _parse_with_blockers(response, resp)[0]
            return response.model_validate(resp.json())
        except ValidationError as exc:
            raise server_answer_mismatch(exc) from exc

    def _call(
        self,
        route: Route[_Request, _Response],
        request: _Request,
    ) -> _Response:
        """Send `request` where `route` is served and read the answer as the route's response."""
        resp = self._send(route.binding, request)
        return self._answer(route.binding, resp, route.response)

    def _probe_health(self) -> httpx.Response | None:
        try:
            return self._send(HEALTH_ROUTE.binding, EmptyRequest())
        except ControlPlaneError:
            return None

    def health(self) -> bool:
        """Whether the server answers `GET /health`. An unreachable server is False, not an error."""
        resp = self._probe_health()
        return resp is not None and 200 <= resp.status_code < 300

    def health_report(self) -> HealthResponse | None:
        """The body of `GET /health`, or None when the server did not answer it with a body this client reads."""
        resp = self._probe_health()
        if resp is None or not 200 <= resp.status_code < 300:
            return None
        try:
            return HealthResponse.model_validate(resp.json())
        except ValueError:
            return None

    def shutdown(self) -> None:
        """Ask the daemon to stop. Raises `ControlPlaneError` when it does not acknowledge."""
        self._call(SHUTDOWN_ROUTE, EmptyRequest())

    def unload(self) -> None:
        self._call(UNLOAD_ROUTE, UnloadRequest())

    def audit_list(
        self, *, action_name: str | None = None, limit: int = 200,
    ) -> Sequence[AuditEntryDTO]:
        request = ListAuditRequest(action_name=action_name, limit=limit)
        return self._call(LIST_AUDIT_ROUTE, request).root

    def events_list(
        self, *, since: int = 0, execution_id: str | None = None, limit: int = 500, tail: bool = False,
    ) -> list[PastEvent]:
        request = ListEventsRequest(since=since, execution_id=execution_id, limit=limit, tail=tail)
        return self._call(LIST_EVENTS_ROUTE, request).root

    def intervention_next(
        self, *, after_seq: int | None, timeout_seconds: float,
    ) -> NextInterventionResponse:
        request = NextInterventionRequest(after_seq=after_seq, timeout_seconds=timeout_seconds)
        return self._call(NEXT_INTERVENTION_ROUTE, request)

    def device_operator_command(self, route: OperatorRoute, req: BaseModel) -> BaseModel:
        """Send an operator device command to the route its Operation is served at, and read its answer."""
        resp = self._send(route.binding, req)
        return self._answer(route.binding, resp, route.response)

    def device_introspection(self, device_id: str) -> DeviceIntrospectionDTO:
        request = GetDeviceIntrospectionRequest(device_id=device_id)
        return self._call(GET_DEVICE_INTROSPECTION_ROUTE, request)

    def access_configs_add(
        self, config: AccessConfigDTO, *, reason: str | None = None,
    ) -> AccessConfigDTO:
        request = CreateAccessConfigRequest(**config.model_dump(), reason=reason)
        answer = self._call(CREATE_ACCESS_CONFIG_ROUTE, request)
        return _convert(AccessConfigDTO, answer)

    def access_configs_update(
        self, config: AccessConfigDTO, *, reason: str | None = None,
    ) -> AccessConfigDTO:
        request = UpdateAccessConfigRequest(**config.model_dump(), reason=reason)
        answer = self._call(UPDATE_ACCESS_CONFIG_ROUTE, request)
        return _convert(AccessConfigDTO, answer)

    def access_configs_get(self, name: str) -> AccessConfigDTO:
        answer = self._call(GET_ACCESS_CONFIG_ROUTE, GetAccessConfigRequest(name=name))
        return _convert(AccessConfigDTO, answer)

    def access_configs_list(self) -> list[AccessConfigDTO]:
        answer = self._call(LIST_ACCESS_CONFIGS_ROUTE, EmptyRequest())
        return [_convert(AccessConfigDTO, x) for x in answer.root]

    def access_configs_delete(self, name: str, *, reason: str | None = None) -> None:
        self._call(DELETE_ACCESS_CONFIG_ROUTE, DeleteAccessConfigRequest(name=name, reason=reason))

    def profiles_list(self) -> list[DeploymentProfile]:
        return self._call(LIST_PROFILES_ROUTE, EmptyRequest()).root

    def profiles_get(self, name: str) -> DeploymentProfile:
        return self._call(GET_PROFILE_ROUTE, ProfileNameRequest(name=name))

    def profiles_add(
        self, profile: DeploymentProfile, *, reason: str | None = None,
    ) -> DeploymentProfile:
        request = SaveProfileRequest(**profile.model_dump(), reason=reason)
        return self._call(CREATE_PROFILE_ROUTE, request)

    def profiles_update(
        self, profile: DeploymentProfile, *, reason: str | None = None,
    ) -> DeploymentProfile:
        request = SaveProfileRequest(**profile.model_dump(), reason=reason)
        return self._call(UPDATE_PROFILE_ROUTE, request)

    def profiles_delete(self, name: str, *, reason: str | None = None) -> None:
        self._call(DELETE_PROFILE_ROUTE, DeleteProfileRequest(name=name, reason=reason))

    def add_labware(
        self,
        *,
        labware_type: str,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntry:
        # Not validated here: the server refuses a bad category and the CLI prints its answer.
        request = LabwareCreateRequest.model_construct(
            labware_type=labware_type, display_name=display_name, category=category,
            vendor=vendor, plr_class_name=plr_class_name, geometry=geometry,
        )
        return self._call(ADD_LABWARE_ENTRY_ROUTE, request)

    @staticmethod
    def _deck_layout(answer: DeckLayoutResponse) -> DeckLayoutDTO:
        return DeckLayoutDTO(device_id=answer.device_id, name=answer.name, config=answer.config)

    def deck_layouts_get(self, device_id: str, name: str) -> DeckLayoutDTO:
        request = GetDeckLayoutRequest(device_id=device_id, name=name)
        return self._deck_layout(self._call(GET_DECK_LAYOUT_ROUTE, request))

    def deck_layouts_save(
        self, device_id: str, name: str, config: DeckLayoutConfig, *, reason: str | None = None,
    ) -> DeckLayoutDTO:
        request = UpdateDeckLayoutRequest(device_id=device_id, name=name, config=config, reason=reason)
        return self._deck_layout(self._call(UPDATE_DECK_LAYOUT_ROUTE, request))

    def deck_layouts_delete(self, device_id: str, name: str, *, reason: str | None = None) -> None:
        request = DeleteDeckLayoutRequest(device_id=device_id, name=name, reason=reason)
        self._call(DELETE_DECK_LAYOUT_ROUTE, request)

    def deck_layouts_list(self, device_id: str | None = None) -> list[DeckLayoutSummaryDTO]:
        request = ListDeckLayoutsRequest(device_id=device_id)
        return self._call(LIST_DECK_LAYOUTS_ROUTE, request).root

    def delete_labware(self, labware_type: str) -> None:
        self._call(DELETE_LABWARE_ENTRY_ROUTE, LabwareEntryRequest(labware_type=labware_type))

    def device_clear_fault(
        self, device_id: str, *, reason: str | None = None,
    ) -> ClearDeviceFaultResponse:
        request = ClearDeviceFaultRequest(device_id=device_id, reason=reason)
        return self._call(CLEAR_DEVICE_FAULT_ROUTE, request)

    def device_compare_deck(self, device_id: str) -> CompareDeckResponse:
        return self._call(COMPARE_DECK_ROUTE, CompareDeckRequest(device_id=device_id))

    def device_confirm_mounted_tips(
        self, device_id: str, *, reason: str | None = None,
    ) -> None:
        request = ConfirmMountedTipsRequest(device_id=device_id, reason=reason)
        self._call(CONFIRM_MOUNTED_TIPS_ROUTE, request)

    def device_get_mounted_tips(self, device_id: str) -> GetMountedTipsResponse:
        request = GetMountedTipsRequest(device_id=device_id)
        return self._call(GET_MOUNTED_TIPS_ROUTE, request)

    def device_reconcile_deck(
        self, device_id: str, *, reason: str | None = None,
    ) -> ReconcileDeckResponse:
        request = ReconcileDeckRequest(device_id=device_id, reason=reason)
        return self._call(RECONCILE_DECK_ROUTE, request)

    def device_release_control(
        self, device_id: str, *, reason: str | None = None,
    ) -> None:
        request = ReleaseDeviceControlRequest(device_id=device_id, reason=reason)
        self._call(RELEASE_DEVICE_CONTROL_ROUTE, request)

    def device_set_mounted_tips(
        self, device_id: str, mounted: list[MountedTipDTO],
        *, reason: str | None = None,
    ) -> None:
        request = SetMountedTipsRequest(device_id=device_id, mounted=mounted, reason=reason)
        self._call(SET_MOUNTED_TIPS_ROUTE, request)

    def device_take_control(
        self, device_id: str, *, reason: str | None = None,
    ) -> None:
        request = TakeDeviceControlRequest(device_id=device_id, reason=reason)
        self._call(TAKE_DEVICE_CONTROL_ROUTE, request)

    def execution_remove(self, execution_id: str, *, reason: str | None = None) -> None:
        request = RemoveExecutionRequest(execution_id=execution_id, reason=reason)
        self._call(REMOVE_EXECUTION_ROUTE, request)

    def get_labware(self, labware_type: str) -> LabwareCatalogEntry:
        return self._call(GET_LABWARE_ENTRY_ROUTE, LabwareEntryRequest(labware_type=labware_type))

    def grip_profiles_get(self, labware_type: str) -> GripProfileDTO:
        request = GetGripProfileRequest(labware_type=labware_type)
        return self._call(GET_GRIP_PROFILE_ROUTE, request)

    def grip_profiles_list(self) -> list[GripProfileDTO]:
        return self._call(LIST_GRIP_PROFILES_ROUTE, EmptyRequest()).root

    def grip_profiles_patch(
        self, labware_type: str, body: GripProfilePatchRequest,
    ) -> GripProfileDTO:
        request = PatchGripProfileRequest(
            labware_type=labware_type, set=body.set, clear=body.clear, reason=body.reason,
        )
        return self._call(PATCH_GRIP_PROFILE_ROUTE, request)

    def grip_profiles_reset(self, labware_type: str, *, reason: str | None = None) -> None:
        request = ResetGripProfileRequest(labware_type=labware_type, reason=reason)
        self._call(RESET_GRIP_PROFILE_ROUTE, request)

    def incidents_ack(
        self, incident_id: str, *, reason: str | None = None,
    ) -> IncidentAckResponse:
        request = AcknowledgeIncidentRequest(incident_id=incident_id, reason=reason)
        return self._call(ACKNOWLEDGE_INCIDENT_ROUTE, request)

    def incidents_ack_all(
        self, category: str | None = None, *, reason: str | None = None,
    ) -> IncidentAckResponse:
        request = AcknowledgeAllIncidentsRequest(category=category, reason=reason)
        return self._call(ACKNOWLEDGE_ALL_INCIDENTS_ROUTE, request)

    def incidents_get(self, incident_id: str) -> IncidentResponse:
        return self._call(GET_INCIDENT_ROUTE, GetIncidentRequest(incident_id=incident_id))

    def labware_clear_carry_override(
        self, labware_id: str, *, reason: str | None = None,
    ) -> None:
        request = ClearLabwareCarryOverrideRequest(labware_id=labware_id, reason=reason)
        self._call(CLEAR_LABWARE_CARRY_OVERRIDE_ROUTE, request)

    def labware_confirm_tip_state(
        self, labware_id: str, reason: str | None = None,
    ) -> None:
        request = ConfirmTipStateRequest(labware_id=labware_id, reason=reason)
        self._call(CONFIRM_TIP_STATE_ROUTE, request)

    def labware_confirm_well_volumes(
        self, labware_id: str, reason: str | None = None,
    ) -> None:
        request = ConfirmWellVolumesRequest(labware_id=labware_id, reason=reason)
        self._call(CONFIRM_WELL_VOLUMES_ROUTE, request)

    def labware_edit_barcode(
        self, labware_id: str, barcode: str, *, reason: str | None = None,
    ) -> None:
        request = EditLabwareBarcodeRequest(labware_id=labware_id, new_barcode=barcode, reason=reason)
        self._call(EDIT_LABWARE_BARCODE_ROUTE, request)

    def labware_edit_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None:
        request = EditLabwareLocationRequest(labware_id=labware_id, location=location, reason=reason)
        self._call(EDIT_LABWARE_LOCATION_ROUTE, request)

    def labware_get_by_barcode(self, barcode: str) -> LabwareSnapshotModel:
        request = GetLabwareByBarcodeRequest(barcode=barcode)
        return self._call(GET_LABWARE_BY_BARCODE_ROUTE, request).labware

    def labware_get_by_barcode_or_none(self, barcode: str) -> LabwareSnapshotModel | None:
        request = GetLabwareByBarcodeRequest(barcode=barcode)
        resp = self._send(GET_LABWARE_BY_BARCODE_ROUTE.binding, request)
        if resp.status_code == HTTPStatus.NOT_FOUND:
            return None
        return self._answer(GET_LABWARE_BY_BARCODE_ROUTE.binding, resp, GET_LABWARE_BY_BARCODE_ROUTE.response).labware

    def labware_get_by_id(self, labware_id: str) -> LabwareSnapshotModel:
        request = GetLabwareByIdRequest(labware_id=labware_id)
        return self._call(GET_LABWARE_BY_ID_ROUTE, request).labware

    def labware_get_by_id_or_none(self, labware_id: str) -> LabwareSnapshotModel | None:
        """None on a 404, so ``labware where`` can try the id and then the barcode."""
        request = GetLabwareByIdRequest(labware_id=labware_id)
        resp = self._send(GET_LABWARE_BY_ID_ROUTE.binding, request)
        if resp.status_code == HTTPStatus.NOT_FOUND:
            return None
        return self._answer(GET_LABWARE_BY_ID_ROUTE.binding, resp, GET_LABWARE_BY_ID_ROUTE.response).labware

    def check_run_labware(self, execution_id: str) -> GetCheckRunLabwareResponse:
        request = GetCheckRunLabwareRequest(execution_id=execution_id)
        return self._call(GET_CHECK_RUN_LABWARE_ROUTE, request)

    def labware_list(self) -> list[LabwareSnapshotModel]:
        return list(self._call(LIST_LABWARE_ROUTE, ListLabwareRequest()).labware)

    def labware_mark_tips_used(
        self, labware_id: str, positions: list[str], reason: str,
    ) -> MarkTipsUsedResponse:
        request = MarkTipsUsedRequest(labware_id=labware_id, positions=positions, reason=reason)
        return self._call(MARK_TIPS_USED_ROUTE, request)

    def labware_register(
        self, template_name: str | None = None,
        labware_type: str | None = None,
        barcode: str | None = None,
        location: str | None = None,
        reason: str | None = None,
    ) -> LabwareSnapshotModel:
        request = RegisterLabwareRequest(
            template_name=template_name, labware_type=labware_type,
            barcode=barcode, location=location, reason=reason,
        )
        return self._call(REGISTER_LABWARE_ROUTE, request).labware

    def labware_reset_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None:
        request = ResetLabwareLocationRequest(labware_id=labware_id, location=location, reason=reason)
        self._call(RESET_LABWARE_LOCATION_ROUTE, request)

    def labware_resolve_contents(self, labware_id: str) -> ResolveContentsResponse:
        request = ResolveContentsRequest(labware_id=labware_id)
        return self._call(RESOLVE_CONTENTS_ROUTE, request)

    def labware_set_carry_override(
        self, labware_id: str, patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (), *, reason: str | None = None,
    ) -> MoveParameterPatch:
        request = SetLabwareCarryOverrideRequest(
            labware_id=labware_id, set=patch, clear=list(clear), reason=reason,
        )
        return self._call(SET_LABWARE_CARRY_OVERRIDE_ROUTE, request).carry_override

    def labware_set_tip_state(
        self, labware_id: str, tip_positions_present: list[str], reason: str,
    ) -> None:
        request = SetTipStateRequest(
            labware_id=labware_id, tip_positions_present=tip_positions_present, reason=reason,
        )
        self._call(SET_TIP_STATE_ROUTE, request)

    def labware_set_well_volumes(
        self, labware_id: str, well_volumes: dict[str, float], reason: str,
    ) -> None:
        request = SetWellVolumesRequest(labware_id=labware_id, well_volumes=well_volumes, reason=reason)
        self._call(SET_WELL_VOLUMES_ROUTE, request)

    def reservations_list(self, execution_id: str | None = None) -> list[ReservationSnapshotDTO]:
        request = ListReservationsRequest(execution_id=execution_id)
        return self._call(LIST_RESERVATIONS_ROUTE, request).root

    def manual_step_confirm(
        self, execution_id: str, step_id: str, *, reason: str | None = None,
    ) -> None:
        request = ConfirmManualStepRequest(execution_id=execution_id, step_id=step_id, reason=reason)
        self._call(CONFIRM_MANUAL_STEP_ROUTE, request)

    def manual_steps_list(self, execution_id: str | None) -> list[PendingManualStepDTO]:
        request = ListPendingManualStepsRequest(execution_id=execution_id)
        return list(self._call(LIST_PENDING_MANUAL_STEPS_ROUTE, request).pending)

    def move_defaults_get(self, device_id: str) -> MoveDefaultsDTO:
        request = GetMoveDefaultsRequest(device_id=device_id)
        return self._call(GET_MOVE_DEFAULTS_ROUTE, request)

    def move_defaults_list(self) -> list[MoveDefaultsDTO]:
        return self._call(LIST_MOVE_DEFAULTS_ROUTE, EmptyRequest()).root

    def move_defaults_patch(
        self, device_id: str, body: MoveDefaultsPatchRequest,
    ) -> MoveDefaultsDTO:
        request = PatchMoveDefaultsRequest(
            device_id=device_id, set=body.set, clear=body.clear, reason=body.reason,
        )
        return self._call(PATCH_MOVE_DEFAULTS_ROUTE, request)

    def move_defaults_reset(self, device_id: str, *, reason: str | None = None) -> None:
        request = ResetMoveDefaultsRequest(device_id=device_id, reason=reason)
        self._call(RESET_MOVE_DEFAULTS_ROUTE, request)

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
    ) -> SearchOpsHistoryResponse:
        request = SearchOpsHistoryRequest(
            execution_id=execution_id,
            action_id=action_id,
            thread_id=thread_id,
            method_id=method_id,
            source=source,
            operation=operation,
            labware_name=labware_name,
            device_name=device_name,
        )
        return self._call(SEARCH_OPS_HISTORY_ROUTE, request)

    def plugins_list(self) -> list[PluginDTO]:
        return self._call(LIST_PLUGINS_ROUTE, ListPluginsRequest()).root

    def recover_thread(
        self, execution_id: str, thread_id: str, decision: str, *, reason: str | None = None,
    ) -> None:
        request = RecoverThreadRequest(
            execution_id=execution_id, thread_id=thread_id, decision=decision, reason=reason,
        )
        self._call(RECOVER_THREAD_ROUTE, request)

    def recoverable_timeout_abort(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse:
        request = AbortRecoverableTimeoutRequest(
            incident_id=incident_id, operator_name=operator_name, reason=reason,
        )
        return self._call(ABORT_RECOVERABLE_TIMEOUT_ROUTE, request)

    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float, *,
        reason: str | None = None,
    ) -> RecoverableTimeoutDecisionResponse:
        request = ExtendRecoverableTimeoutRequest(
            incident_id=incident_id, additional_seconds=additional_seconds, reason=reason,
        )
        return self._call(EXTEND_RECOVERABLE_TIMEOUT_ROUTE, request)

    def recoverable_timeout_mark_complete(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse:
        request = MarkRecoverableTimeoutCompleteRequest(
            incident_id=incident_id, operator_name=operator_name, reason=reason,
        )
        return self._call(MARK_RECOVERABLE_TIMEOUT_COMPLETE_ROUTE, request)

    def release_mover_hold(
        self, device_id: str, reason: str,
        to_location: str | None = None, force: bool = False,
    ) -> ReleaseMoverHoldResponse:
        request = ReleaseMoverHoldRequest(
            device_id=device_id, to_location=to_location, force=force, reason=reason,
        )
        return self._call(RELEASE_MOVER_HOLD_ROUTE, request)

    def runtime_status(self) -> RuntimeStatusResponse:
        return self._call(GET_RUNTIME_STATUS_ROUTE, GetRuntimeStatusRequest())

    def runtime_reload(self, reason: str = "") -> ReloadResponse:
        return self._call(RELOAD_RUNTIME_ROUTE, ReloadRequest(reason=reason))

    def set_execution_sim_speedup(
        self, execution_id: str, sim_speedup: float,
    ) -> SetExecutionSimSpeedupResponse:
        request = SetExecutionSimSpeedupRequest(execution_id=execution_id, sim_speedup=sim_speedup)
        return self._call(SET_EXECUTION_SIM_SPEEDUP_ROUTE, request)

    def state_unsettled(self) -> UnsettledStateResponse:
        return self._call(UNSETTLED_STATE_ROUTE, UnsettledStateRequest())

    def stop_execution(
        self, execution_id: str, *, confirm: bool = False, reason: str | None = None,
    ) -> StopExecutionResponse:
        request = StopExecutionRequest(execution_id=execution_id, confirm=confirm, reason=reason)
        return self._call(STOP_EXECUTION_ROUTE, request)

    def submission_get(self, submission_id: str) -> SubmissionDTO:
        request = GetSubmissionRequest(submission_id=submission_id)
        return self._call(GET_SUBMISSION_ROUTE, request).submission

    def submissions_list(self, execution_id: str | None = None) -> list[SubmissionDTO]:
        request = ListSubmissionsRequest(execution_id=execution_id)
        return list(self._call(LIST_SUBMISSIONS_ROUTE, request).submissions)

    def teachpoints_clear_labware_override(
        self, device_id: str, position_id: str, labware_type: str, *, reason: str | None = None,
    ) -> None:
        request = ClearLabwareOverrideRequest(
            device_id=device_id, name=position_id, labware_type=labware_type, reason=reason,
        )
        self._call(CLEAR_LABWARE_OVERRIDE_ROUTE, request)

    def teachpoints_delete(
        self, device_id: str, position_id: str, *, reason: str | None = None,
    ) -> None:
        request = DeleteTeachpointRequest(device_id=device_id, name=position_id, reason=reason)
        self._call(DELETE_TEACHPOINT_ROUTE, request)

    def teachpoints_create(
        self,
        device_id: str,
        teachpoints: Sequence[dict[str, JsonValue]],
        overwrite: bool = False,
        *, reason: str | None = None,
    ) -> CreateTeachpointsResponse:
        request = CreateTeachpointsRequest(
            device_id=device_id, teachpoints=list(teachpoints), overwrite=overwrite, reason=reason,
        )
        return self._call(CREATE_TEACHPOINTS_ROUTE, request)

    def teachpoints_get(self, device_id: str, position_id: str) -> TeachpointDTO:
        request = GetTeachpointRequest(device_id=device_id, name=position_id)
        return self._call(GET_TEACHPOINT_ROUTE, request)

    def teachpoints_list(self, device_id: str) -> list[TeachpointDTO]:
        request = ListTeachpointsRequest(device_id=device_id)
        return self._call(LIST_TEACHPOINTS_ROUTE, request).root

    def teachpoints_list_all(self) -> list[TeachpointDTO]:
        return self._call(LIST_TEACHPOINTS_ROUTE, ListTeachpointsRequest()).root

    def teachpoints_set_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        body: GripProfilePatchRequest,
    ) -> TeachpointDTO:
        request = SetLabwareOverrideRequest(
            device_id=device_id, name=position_id, labware_type=labware_type,
            set=body.set, clear=body.clear, reason=body.reason,
        )
        return self._call(SET_LABWARE_OVERRIDE_ROUTE, request)

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
        *, reason: str | None = None,
    ) -> TeachpointDTO:
        # Not validated here: the server refuses a bad orientation and the CLI prints its answer.
        request = UpdateTeachpointRequest.model_construct(
            device_id=device_id, name=position_id, coords=dict(coords),
            access_config_name=access_config_name, gateway=gateway, orientation=orientation,
            taught_with=taught_with, update_access_config=update_access_config,
            update_gateway=update_gateway, update_orientation=update_orientation,
            update_taught_with=update_taught_with, reason=reason,
        )
        return self._call(UPDATE_TEACHPOINT_ROUTE, request)

    def thread_insert_action(
        self, execution_id: str, thread_id: str, *,
        action_code: str,
        where: InsertWhere,
        anchor: str | None = None,
        reason: str,
    ) -> None:
        request = InsertActionRequest(
            execution_id=execution_id, thread_id=thread_id, action_code=action_code,
            where=where, anchor=anchor, reason=reason,
        )
        self._call(INSERT_ACTION_ROUTE, request)

    def thread_insert_method(
        self, execution_id: str, thread_id: str, *,
        template_name: str | None = None,
        method_code: str | None = None,
        where: InsertWhere,
        anchor: str | None = None,
        reason: str,
    ) -> None:
        request = InsertMethodRequest(
            execution_id=execution_id, thread_id=thread_id, template_name=template_name,
            method_code=method_code, where=where, anchor=anchor, reason=reason,
        )
        self._call(INSERT_METHOD_ROUTE, request)

    def thread_replace_action(
        self, execution_id: str, thread_id: str, *,
        target_command: str,
        action_code: str,
        reason: str,
    ) -> ReplaceResult:
        request = ReplaceActionRequest(
            execution_id=execution_id, thread_id=thread_id, target_command=target_command,
            action_code=action_code, reason=reason,
        )
        return self._call(REPLACE_ACTION_ROUTE, request)

    def thread_replace_method(
        self, execution_id: str, thread_id: str, *,
        target_name: str,
        template_name: str | None = None,
        method_code: str | None = None,
        reason: str,
    ) -> ReplaceResult:
        request = ReplaceMethodRequest(
            execution_id=execution_id, thread_id=thread_id, target_name=target_name,
            template_name=template_name, method_code=method_code, reason=reason,
        )
        return self._call(REPLACE_METHOD_ROUTE, request)

    def thread_skip_action(
        self, execution_id: str, thread_id: str, *,
        action_id: str | None = None,
        action_command: str | None = None,
        reason: str,
    ) -> None:
        request = SkipActionRequest(
            execution_id=execution_id, thread_id=thread_id, action_id=action_id,
            action_command=action_command, reason=reason,
        )
        self._call(SKIP_ACTION_ROUTE, request)

    def thread_skip_method(
        self, execution_id: str, thread_id: str, *,
        method_name: str,
        reason: str,
    ) -> None:
        request = SkipMethodRequest(
            execution_id=execution_id, thread_id=thread_id, method_name=method_name, reason=reason,
        )
        self._call(SKIP_METHOD_ROUTE, request)

    def topology_get(
        self, source: bool = False,
    ) -> TopologyViewResponse | TopologySourceResponse:
        return self._call(GET_TOPOLOGY_ROUTE, GetTopologyRequest(source=source)).root

    def list_locations(self) -> list[LocationDTO]:
        """The locations `GET /api/topology` carries."""
        view = self.topology_get()
        if isinstance(view, TopologySourceResponse):
            raise ControlPlaneError("the server answered the topology source, not its view")
        return list(view.locations)

    def update_labware(
        self,
        labware_type: str,
        *,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntry:
        # Not validated here: the server refuses a bad category and the CLI prints its answer.
        request = LabwareUpdateRequest.model_construct(
            labware_type=labware_type, display_name=display_name, category=category,
            vendor=vendor, plr_class_name=plr_class_name, geometry=geometry,
        )
        return self._call(UPDATE_LABWARE_ENTRY_ROUTE, request)

    def variables_get(
        self, execution_id: str, name: str, submission_id: str | None = None,
    ) -> VariableValue:
        request = GetVariableRequest(execution_id=execution_id, name=name, submission_id=submission_id)
        return self._call(GET_VARIABLE_ROUTE, request)

    def variables_list(self, execution_id: str) -> dict[str, OptionValueJson]:
        request = ListVariablesRequest(execution_id=execution_id)
        return self._call(LIST_VARIABLES_ROUTE, request).root

    def variables_list_all(self) -> list[CrossExecVariableDTO]:
        return self._call(LIST_ALL_VARIABLES_ROUTE, ListAllVariablesRequest()).root

    def variables_unset(
        self, execution_id: str, name: str, *, reason: str | None = None,
    ) -> None:
        request = UnsetVariableRequest(execution_id=execution_id, name=name, reason=reason)
        self._call(UNSET_VARIABLE_ROUTE, request)

    def variables_unset_submission(
        self, execution_id: str, submission_id: str, name: str, *, reason: str | None = None,
    ) -> None:
        request = UnsetSubmissionVariableRequest(
            execution_id=execution_id, submission_id=submission_id, name=name, reason=reason,
        )
        self._call(UNSET_SUBMISSION_VARIABLE_ROUTE, request)

    def device_registry_list(self) -> list[DeviceRegistryEntryResponse]:
        request = ListDeviceRegistryRequest()
        return list(self._call(LIST_DEVICE_REGISTRY_ROUTE, request).devices)

    def device_registry_show(self, device_id: str) -> DeviceRegistryEntryResponse:
        request = ShowDeviceRegistryEntryRequest(device_id=device_id)
        return self._call(SHOW_DEVICE_REGISTRY_ENTRY_ROUTE, request)

    def get_thread_detail(
        self, execution_id: str, thread_id: str,
    ) -> ThreadSnapshotResponse:
        request = GetThreadRequest(execution_id=execution_id, thread_id=thread_id)
        return self._call(GET_THREAD_ROUTE, request)

    def incidents_list(
        self,
        unacknowledged_only: bool = False,
        category: str | None = None,
        execution_id: str | None = None,
    ) -> list[IncidentResponse]:
        request = ListIncidentsRequest(
            unacknowledged_only=unacknowledged_only, category=category, execution_id=execution_id,
        )
        return self._call(LIST_INCIDENTS_ROUTE, request).root

    def labware_journey(
        self, labware_id: str, kinds: Sequence[str] | None = None,
    ) -> GetLabwareJourneyResponse:
        # Not validated here: the server refuses a kind it does not know and the CLI prints its answer.
        request = GetLabwareJourneyRequest.model_construct(
            labware_id=labware_id, kinds=list(kinds) if kinds is not None else None,
        )
        return self._call(GET_LABWARE_JOURNEY_ROUTE, request)

    def list_executions(self) -> list[ExecutionRecordModel]:
        answer = self._call(LIST_EXECUTIONS_ROUTE, ListExecutionsRequest())
        return list(answer.executions)

    def list_labware(
        self, category: str | None = None,
    ) -> list[LabwareCatalogSummary]:
        # Not validated here: the server refuses a bad category and the CLI prints its answer.
        request = LabwareCatalogRequest.model_construct(category=category)
        return list(self._call(GET_LABWARE_CATALOG_ROUTE, request).labware)

    def method_get(
        self, name: str, workflow_name: str | None = None,
    ) -> MethodTemplateDTO:
        request = GetMethodRequest(name=name, workflow_name=workflow_name)
        return self._call(GET_METHOD_ROUTE, request).method

    def methods_list(self) -> list[MethodTemplateDTO]:
        return list(self._call(LIST_METHODS_ROUTE, EmptyRequest()).methods)

    def pause_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> None:
        request = PauseRequest(scope=ExecutionScope(execution_id=execution_id), reason=reason)
        self._call(PAUSE_ROUTE, request)

    def pause_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None:
        request = PauseRequest(
            scope=ThreadScope(execution_id=execution_id, thread_id=thread_id), reason=reason,
        )
        self._call(PAUSE_ROUTE, request)

    def reservation_cancel(
        self, execution_id: str, reservation_id: str, reason: str,
    ) -> None:
        request = CancelReservationRequest(
            execution_id=execution_id, reservation_id=reservation_id, reason=reason,
        )
        self._call(CANCEL_RESERVATION_ROUTE, request)

    def resume_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> ResumeExecutionResult:
        request = ResumeRequest(scope=ExecutionScope(execution_id=execution_id), reason=reason)
        result = self._call(RESUME_ROUTE, request).result
        if not isinstance(result, ResumeExecutionResult):
            raise ControlPlaneError(
                "the server answered a resume of one thread, not of the whole execution",
            )
        return result

    def resume_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None:
        request = ResumeRequest(
            scope=ThreadScope(execution_id=execution_id, thread_id=thread_id), reason=reason,
        )
        self._call(RESUME_ROUTE, request)

    def spawn_thread(
        self, execution_id: str, template_name: str,
        labware_id: str | None = None, *, reason: str | None = None,
    ) -> ThreadSnapshotResponse:
        request = SpawnThreadRequest(
            execution_id=execution_id, template_name=template_name, labware_id=labware_id, reason=reason,
        )
        spawned = self._call(SPAWN_THREAD_ROUTE, request)
        return _convert(ThreadSnapshotResponse, spawned)

    def submit_method(
        self,
        workflow_name: str,
        method_name: str,
        labware_start: Mapping[str, str],
        labware_end: Mapping[str, str],
        variables: Mapping[str, OptionValueJson] | None = None,
        *,
        run_mode: RunModeStr,
        deployment_profile: str | None = None,
        event_hooks: Sequence[str] | None = None,
        sim_speedup: float | None = None,
    ) -> MethodExecutionResponse:
        request = MethodExecutionRequest(
            workflow_name=workflow_name,
            method_name=method_name,
            labware_start=dict(labware_start),
            labware_end=dict(labware_end),
            variables=dict(variables) if variables is not None else {},
            deployment_profile=deployment_profile,
            run_mode=run_mode,
            event_hooks=None if event_hooks is None else list(event_hooks),
            sim_speedup=sim_speedup,
        )
        return self._call(SUBMIT_METHOD_EXECUTION_ROUTE, request)

    def variables_set(
        self, execution_id: str, name: str, value: OptionValueJson,
        *, reason: str | None = None,
    ) -> VariableSetResponse:
        request = SetVariableRequest(execution_id=execution_id, name=name, value=value, reason=reason)
        return self._call(SET_VARIABLE_ROUTE, request)

    def variables_get_global(self, name: str) -> VariableValue:
        return self._call(GET_GLOBAL_VARIABLE_ROUTE, GetGlobalVariableRequest(name=name))

    def variables_unset_global(self, name: str, *, reason: str | None = None) -> bool:
        """True when a set value was removed; a declared global then reads its default again."""
        request = UnsetGlobalVariableRequest(name=name, reason=reason)
        return self._call(UNSET_GLOBAL_VARIABLE_ROUTE, request).existed

    def variables_set_global(
        self, name: str, value: OptionValueJson, *, reason: str | None = None,
    ) -> None:
        request = SetGlobalVariableRequest(name=name, value=value, reason=reason)
        self._call(SET_GLOBAL_VARIABLE_ROUTE, request)

    def variables_set_submission(
        self, execution_id: str, submission_id: str, name: str,
        value: OptionValueJson, *, reason: str | None = None,
    ) -> None:
        request = SetSubmissionVariableRequest(
            execution_id=execution_id, submission_id=submission_id, name=name, value=value, reason=reason,
        )
        self._call(SET_SUBMISSION_VARIABLE_ROUTE, request)

    def workflow_get(self, name: str) -> WorkflowTemplateDTO:
        return self._call(GET_WORKFLOW_ROUTE, GetWorkflowRequest(name=name)).workflow

    def device_connect(
        self, device_id: str, *,
        reason: str | None = None,
    ) -> None:
        request = ConnectDeviceRequest(device_id=device_id, reason=reason)
        self._call(CONNECT_DEVICE_ROUTE, request)

    def device_set_mode(
        self, device_id: str, mode: DeviceMode, *, confirm: bool = False,
    ) -> SetDeviceModeResponse:
        request = SetDeviceModeRequest(device_id=device_id, mode=mode, confirm=confirm)
        return self._call(SET_DEVICE_MODE_ROUTE, request)

    def device_setup_get(self, device_id: str) -> DeviceSetupResponse:
        request = GetDeviceSetupRequest(device_id=device_id)
        return self._call(GET_DEVICE_SETUP_ROUTE, request)

    def device_setup_put(
        self, device_id: str, setup: DeviceSetup, *, reason: str | None = None,
    ) -> DeviceSetupResponse:
        request = PutDeviceSetupRequest(device_id=device_id, setup=setup, reason=reason)
        return self._call(PUT_DEVICE_SETUP_ROUTE, request)

    def device_disconnect(
        self, device_id: str, *,
        reason: str | None = None,
    ) -> None:
        request = DisconnectDeviceRequest(device_id=device_id, reason=reason)
        self._call(DISCONNECT_DEVICE_ROUTE, request)

    def device_initialize(
        self, device_id: str, *,
        reason: str | None = None,
    ) -> None:
        request = InitializeDeviceRequest(device_id=device_id, reason=reason)
        self._call(INITIALIZE_DEVICE_ROUTE, request)

    def labware_clear_all(
        self, force: bool = False, *, reason: str | None = None,
    ) -> dict[str, list[str]]:
        request = ClearAllLabwareRequest(force=force, reason=reason)
        response = self._call(CLEAR_ALL_LABWARE_ROUTE, request)
        return {"cleared_labware_ids": list(response.cleared)}

    def labware_load_run(self, request: LoadRunLabwareRequest) -> LoadRunLabwareResponse:
        return self._call(LOAD_RUN_LABWARE_ROUTE, request)

    def labware_clear_submission(
        self, submission_id: str, force: bool = False, *, reason: str | None = None,
    ) -> dict[str, list[str]]:
        request = ClearSubmissionLabwareRequest(submission_id=submission_id, force=force, reason=reason)
        response = self._call(CLEAR_SUBMISSION_LABWARE_ROUTE, request)
        return {
            "cleared": list(response.cleared),
            "preserved_reuse_bound": list(response.preserved_reuse_bound),
        }

    def labware_discharge(
        self, labware_id: str, force: bool = False, *, reason: str | None = None,
    ) -> dict[str, list[str]]:
        request = DischargeLabwareRequest(labware_id=labware_id, force=force, reason=reason)
        response = self._call(DISCHARGE_LABWARE_ROUTE, request)
        return {"cleared_labware_ids": [response.labware_id]}

    def list_thread_templates(self) -> list[ThreadTemplateDTO]:
        answer = self._call(LIST_THREAD_TEMPLATES_ROUTE, EmptyRequest())
        return list(answer.threads)

    def list_workflows(self) -> list[WorkflowTemplateDTO]:
        return list(self._call(LIST_WORKFLOWS_ROUTE, EmptyRequest()).workflows)

    def system_info(self) -> GetSystemInfoResponse:
        return self._call(GET_SYSTEM_INFO_ROUTE, GetSystemInfoRequest())

    def submit_workflow(
        self, workflow_name: str,
        variables: Mapping[str, OptionValue] | None = None,
        *,
        run_mode: RunModeStr,
        deployment_profile: str | None = None,
        sim_speedup: float | None = None,
    ) -> ExecutionRecordModel:
        request = SubmitExecutionRequest(
            workflow_name=workflow_name,
            variables=dict(variables) if variables is not None else None,
            run_mode=run_mode,
            deployment_profile=deployment_profile,
            sim_speedup=sim_speedup,
        )
        submitted = self.submission_submit(request)
        return ExecutionRecordModel(
            id=submitted.execution_id,
            workflow_name=submitted.workflow_name,
            status=submitted.status,
            error=None,
            simulated=submitted.run_mode == "SIM",
            sim_speedup=submitted.sim_speedup,
        )

    def submission_submit(
        self, request: SubmitExecutionRequest,
    ) -> SubmitExecutionResponse:
        return self._call(SUBMIT_EXECUTION_ROUTE, request)

    def get_execution(self, execution_id: str) -> GetExecutionDetailResponse:
        request = GetExecutionDetailRequest(execution_id=execution_id)
        return self._call(GET_EXECUTION_DETAIL_ROUTE, request)

    def execution_close(
        self, execution_id: str, *, reason: str | None = None,
    ) -> CloseExecutionResponse:
        request = CloseExecutionRequest(execution_id=execution_id, reason=reason)
        return self._call(CLOSE_EXECUTION_ROUTE, request)
