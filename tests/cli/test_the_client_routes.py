"""The one CLI client: every method's route, and the verb tree.

`ROUTE_CATALOG` names the Operation each `IControlPlaneClient` method reaches.
The method and path expected come from that Operation's binding, and the query
or body sent must be a whole Request of the Operation. A coverage
test fails if a Protocol method has no entry. The verb inventory enumerates the
live Typer tree and fails by name when a verb is added, renamed or removed.
"""

import inspect
import json
import re
from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable

import httpx

from orca.modes.device_modes import DeviceMode
from orca.operations.blocker_models import payload_of
from pydantic import JsonValue
import pytest
import typer
from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.move_parameters import (
    SEED_MOVE_PARAMETERS,
    MoveParameterPatch,
)
from cheshire_drivers.teachpoints import AccessConfig
from orca.operations.device_models import MountedTipDTO
from orca.operations.operator_device_models import MOVE_TO, MoveToRequest

from orca.cli import app as cli_app_module
from orca.cli.app import app as cli_app
from orca.daemon.operation_catalog import DaemonOperationCatalog
from orca.operations.access_configs import (
    CreateAccessConfigOperation,
    DeleteAccessConfigOperation,
    GetAccessConfigOperation,
    ListAccessConfigsOperation,
    UpdateAccessConfigOperation,
)
from orca.operations.audit import ListAuditOperation
from orca.operations.catalog import (
    GetMethodOperation,
    GetWorkflowOperation,
    ListMethodsOperation,
    ListThreadTemplatesOperation,
    ListWorkflowsOperation,
)
from orca.operations.check_runs import GetCheckRunLabwareOperation
from orca.operations.deck_layouts import (
    DeleteDeckLayoutOperation,
    GetDeckLayoutOperation,
    ListDeckLayoutsOperation,
    UpdateDeckLayoutOperation,
)
from orca.operations.device import (
    ClearDeviceFaultOperation,
    CompareDeckOperation,
    ConfirmMountedTipsOperation,
    ConnectDeviceOperation,
    DisconnectDeviceOperation,
    GetMountedTipsOperation,
    InitializeDeviceOperation,
    ReconcileDeckOperation,
    ReleaseDeviceControlOperation,
    SetDeviceModeOperation,
    SetMountedTipsOperation,
    TakeDeviceControlOperation,
)
from orca.operations.device_commands import GetDeviceIntrospectionOperation
from orca.operations.device_registry import ListDeviceRegistryOperation, ShowDeviceRegistryEntryOperation
from orca.operations.device_setups import GetDeviceSetupOperation, PutDeviceSetupOperation
from orca.operations.events import ListEventsOperation
from orca.operations.execution import (
    CloseExecutionOperation,
    GetExecutionDetailOperation,
    ListExecutionsOperation,
    RemoveExecutionOperation,
    SetExecutionSimSpeedupOperation,
    StopExecutionOperation,
)
from orca.operations.grip_profiles import (
    GetGripProfileOperation,
    ListGripProfilesOperation,
    PatchGripProfileOperation,
    ResetGripProfileOperation,
)
from orca.operations.incidents import (
    AbortRecoverableTimeoutOperation,
    AcknowledgeAllIncidentsOperation,
    AcknowledgeIncidentOperation,
    ExtendRecoverableTimeoutOperation,
    GetIncidentOperation,
    ListIncidentsOperation,
    MarkRecoverableTimeoutCompleteOperation,
)
from orca.operations.interventions import NextInterventionOperation
from orca.operations.labware import (
    ClearAllLabwareOperation,
    ClearLabwareCarryOverrideOperation,
    ClearSubmissionLabwareOperation,
    ConfirmTipStateOperation,
    ConfirmWellVolumesOperation,
    DischargeLabwareOperation,
    EditLabwareBarcodeOperation,
    EditLabwareLocationOperation,
    GetLabwareByBarcodeOperation,
    GetLabwareByIdOperation,
    GetLabwareJourneyOperation,
    ListLabwareOperation,
    MarkTipsUsedOperation,
    RegisterLabwareOperation,
    ReleaseMoverHoldOperation,
    ResetLabwareLocationOperation,
    ResolveContentsOperation,
    SetLabwareCarryOverrideOperation,
    SetTipStateOperation,
    SetWellVolumesOperation,
)
from orca.operations.labware_catalog import (
    AddLabwareEntryOperation,
    DeleteLabwareEntryOperation,
    GetLabwareCatalogOperation,
    GetLabwareEntryOperation,
    UpdateLabwareEntryOperation,
)
from orca.operations.manual_step import ConfirmManualStepOperation, ListPendingManualStepsOperation
from orca.operations.move_defaults import (
    GetMoveDefaultsOperation,
    ListMoveDefaultsOperation,
    PatchMoveDefaultsOperation,
    ResetMoveDefaultsOperation,
)
from orca.operations.operator_device import TransporterMoveToOperation
from orca.operations.ops_history import SearchOpsHistoryOperation
from orca.operations.profiles import (
    CreateProfileOperation,
    DeleteProfileOperation,
    GetProfileOperation,
    ListProfilesOperation,
    UpdateProfileOperation,
)
from orca.operations.run_loading import LoadRunLabwareOperation
from orca.operations.runtime_reads import (
    CancelReservationOperation,
    GetThreadOperation,
    ListReservationsOperation,
    ListPluginsOperation,
    ListReservationsOperation,
)
from orca.operations.runtime_status import GetRuntimeStatusOperation
from orca.operations.state import UnsettledStateOperation
from orca.operations.submission import (
    GetSubmissionOperation,
    ListSubmissionsOperation,
    SubmitExecutionOperation,
    SubmitMethodExecutionOperation,
)
from orca.operations.system import GetSystemInfoOperation
from orca.operations.system_loading import ReloadRuntimeOperation
from orca.operations.teachpoint_transfer import CreateTeachpointsOperation
from orca.operations.teachpoints import (
    ClearLabwareOverrideOperation,
    DeleteTeachpointOperation,
    GetTeachpointOperation,
    ListTeachpointsOperation,
    SetLabwareOverrideOperation,
    UpdateTeachpointOperation,
)
from orca.operations.thread import (
    InsertActionOperation,
    InsertMethodOperation,
    PauseOperation,
    RecoverThreadOperation,
    ReplaceActionOperation,
    ReplaceMethodOperation,
    ResumeOperation,
    SkipActionOperation,
    SkipMethodOperation,
    SpawnThreadOperation,
)
from orca.operations.topology import GetTopologyOperation
from orca.operations.variables import (
    GetVariableOperation,
    ListAllVariablesOperation,
    ListVariablesOperation,
    SetGlobalVariableOperation,
    SetSubmissionVariableOperation,
    SetVariableOperation,
    UnsetSubmissionVariableOperation,
    UnsetVariableOperation,
)
from orca.operations.binding import API_PREFIX, BoundOperation, RestBinding
from orca.cli.client import OrcaClient
from orca.cli.daemon_client import DaemonLoadingClient
from orca.cli.control_plane import (
    IControlPlaneClient,
)
from orca.operations.wire_models import (
    DeviceIntrospectionDTO,
)
from orca.operations.execution_models import (
    GetExecutionDetailResponse,
    ExecutionRecordModel,
)
from orca.operations.wire_models import (
    GripProfilePatchRequest,
    MoveDefaultsPatchRequest,
)
from orca.operations.submission_models import SubmitExecutionRequest
from orca.operations.run_loading_models import LoadRunLabwareRequest
from orca.variables.deployment_profile import DeploymentProfile



def _sample_submission_request() -> SubmitExecutionRequest:
    return SubmitExecutionRequest(workflow_name="smc", run_mode="CHECK_RUN")


def _server_handler(request: httpx.Request) -> httpx.Response:
    method, path = request.method, request.url.path
    if request.headers.get("x-api-key") not in (None, "k"):
        return httpx.Response(401, json={"detail": "bad api key"})
    if method == "GET" and path == "/health":
        return httpx.Response(200, json={"status": "ok", "runtime_ready": True})
    if method == "GET" and path == "/api/executions":
        return httpx.Response(200, json={"executions": [
            {"id": "exec-1", "workflow_name": "smc", "status": "running"},
        ]})
    if method == "GET" and path == "/api/devices/shk1/introspection":
        return httpx.Response(200, json={
            "name": "shk1",
            "type": "Shaker",
            "interfaces": ["IShaker"],
            "capabilities": ["shaker"],
            "provides_state": False,
            "methods": {
                "shake": {"kind": "method", "returns": None, "params": {}},
            },
            "danger_levels": {"shaker.shake": "PHYSICAL"},
        })
    if method == "GET" and path.startswith("/api/executions/"):
        eid = path.removeprefix("/api/executions/")
        # Worst-case sparse hosted-server response: missing thread fields.
        return httpx.Response(200, json={
            "id": eid, "workflow_name": "smc", "status": "queued", "sim_speedup": 0.5,
        })
    return httpx.Response(404, json={"detail": f"unmocked: {method} {path}"})


class _Fake(OrcaClient):
    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        api_key: str | None = None,
    ) -> None:
        super().__init__("http://test-server", api_key)
        self._handler = handler

    def _make_http_client(self) -> httpx.Client:
        headers = {} if self._api_key is None else {"X-API-Key": self._api_key}
        return httpx.Client(
            transport=httpx.MockTransport(self._handler),
            base_url=self._base_url, headers=headers, timeout=self._timeout,
        )


def _wire_local(handler: Callable[[httpx.Request], httpx.Response] = _server_handler) -> _Fake:
    return _Fake(handler)


def _wire_cloud(handler: Callable[[httpx.Request], httpx.Response] = _server_handler) -> _Fake:
    return _Fake(handler, api_key="k")


# -- Answers on each server ----------------------------------------------------


def test_health_is_true_on_a_healthy_server() -> None:
    assert _wire_local().health() is True
    assert _wire_cloud().health() is True


def test_execution_list_reads_as_execution_records() -> None:
    local_records = list(_wire_local().list_executions())
    cloud_records = list(_wire_cloud().list_executions())
    assert len(local_records) == 1 and len(cloud_records) == 1
    for rec in (local_records[0], cloud_records[0]):
        assert isinstance(rec, ExecutionRecordModel)
        assert rec.id == "exec-1"
        assert rec.workflow_name == "smc"
        assert rec.status == "running"
        assert isinstance(rec.status, str)


def test_device_introspection_reads_each_servers_shape() -> None:
    """`DeviceIntrospectionDTO` parses both backends' real wire shape.

    A hosted server's `DeviceCapabilitiesResponse` emits
    `name` + `type` and NO `device_id`, so a DTO requiring `device_id`
    raised ValidationError against the cloud backend. Both backends now
    send the same `name` + `type` shape and parse cleanly.
    """
    local_info = _wire_local().device_introspection("shk1")
    cloud_info = _wire_cloud().device_introspection("shk1")
    for info in (local_info, cloud_info):
        assert isinstance(info, DeviceIntrospectionDTO)
        assert info.name == "shk1"
        assert info.type == "Shaker"
        assert info.interfaces == ("IShaker",)
        assert info.capabilities == ("shaker",)
        assert info.provides_state is False
        assert "shake" in info.methods


def test_get_execution_dto_normalization_per_d2() -> None:
    """D2: missing fields in cloud `/detail` default to None / empty on Protocol DTO.

    A hosted server that has not yet implemented thread snapshots returns the
    detail endpoint with no thread fields; the Protocol DTO defaults to []
    threads and 0 counts, not a KeyError.
    """
    detail = _wire_cloud().get_execution("exec-x")
    assert isinstance(detail, GetExecutionDetailResponse)
    assert detail.threads == []
    assert detail.total_thread_count == 0
    assert detail.completed_thread_count == 0
    assert detail.active_thread_count == 0
    assert detail.error is None


def test_get_execution_carries_the_speedup_the_hosted_detail_reports() -> None:
    assert _wire_cloud().get_execution("exec-x").sim_speedup == 0.5


# -- HTTP route catalog --------------------------------------------------------


_SAMPLE_ACCESS_CONFIG = AccessConfig(
    name="ac1", access_type="vertical", gripper_offset=20.0,
    vertical_clearance=20.0, horizontal_clearance=100.0,
)
_SAMPLE_DECK_CONFIG = DeckLayoutConfig(deck_type="BRAVO_96", resources=[])
_SAMPLE_PROFILE = DeploymentProfile(name="qc", variables={"speed": 5})
_SAMPLE_GRIP_PROFILE_PATCH = GripProfilePatchRequest(
    set=MoveParameterPatch(resource_width=76.0),
)
_SAMPLE_GRIP_PROFILE_RECORD = {
    "labware_type": "costar_96",
    "patch": {"resource_width": 76.0},
}
_SAMPLE_MOVE_DEFAULTS_PATCH = MoveDefaultsPatchRequest(
    set=MoveParameterPatch(travel_margin=25.0),
)
_SAMPLE_MOVE_DEFAULTS_RECORD = {
    "transporter_name": "pf400",
    "parameters": SEED_MOVE_PARAMETERS.model_dump(),
    "sources": dict.fromkeys(SEED_MOVE_PARAMETERS.model_dump(), "seed"),
}
_NO_BLOCKERS = payload_of([]).model_dump(mode="json")

_SAMPLE_CARTESIAN_COORDS = {
    "x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0,
}


@dataclass(frozen=True)
class _RouteSpec:
    method_name: str  # the IControlPlaneClient method name
    invoke: Callable[[IControlPlaneClient], object]  # how to call it with sample args
    # The Operation the call reaches. None for a route outside the Operation catalog, given as http_method and path.
    operation: type[BoundOperation] | None = None
    path_values: Mapping[str, str] = field(default_factory=dict)  # the sample values the call puts in the path
    http_method: str = ""
    path: str = ""

    def expected_route(self) -> tuple[str, str]:
        """The method and path the Operation's own binding says, never typed here."""
        if self.operation is None:
            return self.http_method, self.path
        binding = self.operation.binding
        return binding.method, binding.fill_url(self.path_values)[0]


# Sample arguments are fixed so each call sends the same request every run.
# `test_each_call_sends_a_request_its_operation_accepts` validates the query or
# body the call sent against the Operation's Request.
ROUTE_CATALOG: tuple[_RouteSpec, ...] = (
    _RouteSpec(
        method_name="execution_close",
        invoke=lambda c: c.execution_close("exec-1"),
        operation=CloseExecutionOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="pause_all_threads",
        invoke=lambda c: c.pause_all_threads("exec-1"),
        operation=PauseOperation,
    ),
    _RouteSpec(
        method_name="resume_all_threads",
        invoke=lambda c: c.resume_all_threads("exec-1"),
        operation=ResumeOperation,
    ),
    _RouteSpec(
        method_name="manual_steps_list",
        invoke=lambda c: c.manual_steps_list(None),
        operation=ListPendingManualStepsOperation,
    ),
    _RouteSpec(
        method_name="manual_step_confirm",
        invoke=lambda c: c.manual_step_confirm("exec-1", "step-1"),
        operation=ConfirmManualStepOperation, path_values={"execution_id": "exec-1", "step_id": "step-1"},
    ),
    _RouteSpec(
        method_name="incidents_list",
        invoke=lambda c: list(c.incidents_list()),
        operation=ListIncidentsOperation,
    ),
    _RouteSpec(
        method_name="incidents_get",
        invoke=lambda c: c.incidents_get("inc-1"),
        operation=GetIncidentOperation, path_values={"incident_id": "inc-1"},
    ),
    _RouteSpec(
        method_name="incidents_ack",
        invoke=lambda c: c.incidents_ack("inc-1"),
        operation=AcknowledgeIncidentOperation, path_values={"incident_id": "inc-1"},
    ),
    _RouteSpec(
        method_name="incidents_ack_all",
        invoke=lambda c: c.incidents_ack_all(),
        operation=AcknowledgeAllIncidentsOperation,
    ),
    _RouteSpec(
        method_name="list_workflows",
        invoke=lambda c: list(c.list_workflows()),
        operation=ListWorkflowsOperation,
    ),
    _RouteSpec(
        method_name="release_mover_hold",
        invoke=lambda c: c.release_mover_hold("pf400", "stranded by an abort", to_location="pad_1"),
        operation=ReleaseMoverHoldOperation, path_values={"device_id": "pf400"},
    ),
    _RouteSpec(
        method_name="health",
        invoke=lambda c: c.health(),
        http_method="GET", path="/health",
    ),
    _RouteSpec(
        method_name="submit_workflow",
        invoke=lambda c: c.submit_workflow("smc", {"x": 1}, run_mode="CHECK_RUN"),
        operation=SubmitExecutionOperation,
    ),
    _RouteSpec(
        method_name="list_executions",
        invoke=lambda c: list(c.list_executions()),
        operation=ListExecutionsOperation,
    ),
    _RouteSpec(
        method_name="get_execution",
        invoke=lambda c: c.get_execution("exec-1"),
        operation=GetExecutionDetailOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="stop_execution",
        invoke=lambda c: c.stop_execution("exec-1"),
        operation=StopExecutionOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="set_execution_sim_speedup",
        invoke=lambda c: c.set_execution_sim_speedup("exec-1", 0.5),
        operation=SetExecutionSimSpeedupOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="execution_remove",
        invoke=lambda c: c.execution_remove("exec-1"),
        operation=RemoveExecutionOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="device_introspection",
        invoke=lambda c: c.device_introspection("shk1"),
        operation=GetDeviceIntrospectionOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="get_thread_detail",
        invoke=lambda c: c.get_thread_detail("exec-1", "thr-1"),
        operation=GetThreadOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="device_registry_list",
        invoke=lambda c: list(c.device_registry_list()),
        operation=ListDeviceRegistryOperation,
    ),
    _RouteSpec(
        method_name="device_registry_show",
        invoke=lambda c: c.device_registry_show("shk1"),
        operation=ShowDeviceRegistryEntryOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="thread_skip_method",
        invoke=lambda c: c.thread_skip_method(
            "exec-1", "thr-1", method_name="incubate", reason="test",
        ),
        operation=SkipMethodOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="thread_insert_method",
        invoke=lambda c: c.thread_insert_method(
            "exec-1", "thr-1", template_name="m1", where="tail", reason="test",
        ),
        operation=InsertMethodOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="thread_skip_action",
        invoke=lambda c: c.thread_skip_action(
            "exec-1", "thr-1", action_id="act-1", reason="test",
        ),
        operation=SkipActionOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="thread_insert_action",
        invoke=lambda c: c.thread_insert_action(
            "exec-1", "thr-1", action_code="@orca.action\ndef a(): pass\n",
            where="tail", reason="test",
        ),
        operation=InsertActionOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="thread_replace_method",
        invoke=lambda c: c.thread_replace_method(
            "exec-1", "thr-1", target_name="m1", template_name="m2", reason="test",
        ),
        operation=ReplaceMethodOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="thread_replace_action",
        invoke=lambda c: c.thread_replace_action(
            "exec-1", "thr-1", target_command="seal",
            action_code="@orca.action\ndef a(): pass\n", reason="test",
        ),
        operation=ReplaceActionOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="audit_list",
        invoke=lambda c: list(c.audit_list()),
        operation=ListAuditOperation,
    ),
    _RouteSpec(
        method_name="events_list",
        invoke=lambda c: list(c.events_list()),
        operation=ListEventsOperation,
    ),
    _RouteSpec(
        method_name="intervention_next",
        invoke=lambda c: c.intervention_next(after_seq=3, timeout_seconds=1.0),
        operation=NextInterventionOperation,
    ),
    _RouteSpec(
        method_name="submit_method",
        invoke=lambda c: c.submit_method(
            workflow_name="hello",
            method_name="incubate",
            labware_start={"sample_plate": "pad_1"},
            labware_end={"sample_plate": "pad_1"},
            run_mode="CHECK_RUN",
        ),
        operation=SubmitMethodExecutionOperation,
    ),
    _RouteSpec(
        method_name="variables_list",
        invoke=lambda c: c.variables_list("exec-1"),
        operation=ListVariablesOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="variables_get",
        invoke=lambda c: c.variables_get("exec-1", "n"),
        operation=GetVariableOperation, path_values={"execution_id": "exec-1", "name": "n"},
    ),
    _RouteSpec(
        method_name="variables_set",
        invoke=lambda c: c.variables_set("exec-1", "n", 1),
        operation=SetVariableOperation, path_values={"execution_id": "exec-1", "name": "n"},
    ),
    _RouteSpec(
        method_name="variables_set_submission",
        invoke=lambda c: c.variables_set_submission("exec-1", "sub-1", "n", 1),
        operation=SetSubmissionVariableOperation, path_values={"execution_id": "exec-1", "submission_id": "sub-1", "name": "n"},
    ),
    _RouteSpec(
        method_name="variables_unset_submission",
        invoke=lambda c: c.variables_unset_submission("exec-1", "sub-1", "n"),
        operation=UnsetSubmissionVariableOperation, path_values={"execution_id": "exec-1", "submission_id": "sub-1", "name": "n"},
    ),
    _RouteSpec(
        method_name="variables_set_global",
        invoke=lambda c: c.variables_set_global("n", 1),
        operation=SetGlobalVariableOperation, path_values={"name": "n"},
    ),
    _RouteSpec(
        method_name="variables_get_global",
        invoke=lambda c: c.variables_get_global("n"),
        http_method="GET", path="/api/variables/global/n",
    ),
    _RouteSpec(
        method_name="variables_unset_global",
        invoke=lambda c: c.variables_unset_global("n"),
        http_method="DELETE", path="/api/variables/global/n",
    ),
    _RouteSpec(
        method_name="variables_unset",
        invoke=lambda c: c.variables_unset("exec-1", "n"),
        operation=UnsetVariableOperation, path_values={"execution_id": "exec-1", "name": "n"},
    ),
    _RouteSpec(
        method_name="variables_list_all",
        invoke=lambda c: list(c.variables_list_all()),
        operation=ListAllVariablesOperation,
    ),
    _RouteSpec(
        method_name="profiles_list",
        invoke=lambda c: list(c.profiles_list()),
        operation=ListProfilesOperation,
    ),
    _RouteSpec(
        method_name="profiles_get",
        invoke=lambda c: c.profiles_get("qc"),
        operation=GetProfileOperation, path_values={"name": "qc"},
    ),
    _RouteSpec(
        method_name="profiles_add",
        invoke=lambda c: c.profiles_add(_SAMPLE_PROFILE),
        operation=CreateProfileOperation,
    ),
    _RouteSpec(
        method_name="profiles_update",
        invoke=lambda c: c.profiles_update(_SAMPLE_PROFILE),
        operation=UpdateProfileOperation, path_values={"name": "qc"},
    ),
    _RouteSpec(
        method_name="profiles_delete",
        invoke=lambda c: c.profiles_delete("qc"),
        operation=DeleteProfileOperation, path_values={"name": "qc"},
    ),
    _RouteSpec(
        method_name="access_configs_list",
        invoke=lambda c: list(c.access_configs_list()),
        operation=ListAccessConfigsOperation,
    ),
    _RouteSpec(
        method_name="access_configs_get",
        invoke=lambda c: c.access_configs_get("ac1"),
        operation=GetAccessConfigOperation, path_values={"name": "ac1"},
    ),
    _RouteSpec(
        method_name="access_configs_add",
        invoke=lambda c: c.access_configs_add(_SAMPLE_ACCESS_CONFIG),
        operation=CreateAccessConfigOperation,
    ),
    _RouteSpec(
        method_name="access_configs_update",
        invoke=lambda c: c.access_configs_update(_SAMPLE_ACCESS_CONFIG),
        operation=UpdateAccessConfigOperation, path_values={"name": "ac1"},
    ),
    _RouteSpec(
        method_name="access_configs_delete",
        invoke=lambda c: c.access_configs_delete("ac1"),
        operation=DeleteAccessConfigOperation, path_values={"name": "ac1"},
    ),
    _RouteSpec(
        method_name="grip_profiles_list",
        invoke=lambda c: list(c.grip_profiles_list()),
        operation=ListGripProfilesOperation,
    ),
    _RouteSpec(
        method_name="grip_profiles_get",
        invoke=lambda c: c.grip_profiles_get("costar_96"),
        operation=GetGripProfileOperation, path_values={"labware_type": "costar_96"},
    ),
    _RouteSpec(
        method_name="grip_profiles_patch",
        invoke=lambda c: c.grip_profiles_patch("costar_96", _SAMPLE_GRIP_PROFILE_PATCH),
        operation=PatchGripProfileOperation, path_values={"labware_type": "costar_96"},
    ),
    _RouteSpec(
        method_name="grip_profiles_reset",
        invoke=lambda c: c.grip_profiles_reset("costar_96"),
        operation=ResetGripProfileOperation, path_values={"labware_type": "costar_96"},
    ),
    _RouteSpec(
        method_name="move_defaults_list",
        invoke=lambda c: list(c.move_defaults_list()),
        operation=ListMoveDefaultsOperation,
    ),
    _RouteSpec(
        method_name="move_defaults_get",
        invoke=lambda c: c.move_defaults_get("pf400"),
        operation=GetMoveDefaultsOperation, path_values={"device_id": "pf400"},
    ),
    _RouteSpec(
        method_name="move_defaults_patch",
        invoke=lambda c: c.move_defaults_patch("pf400", _SAMPLE_MOVE_DEFAULTS_PATCH),
        operation=PatchMoveDefaultsOperation, path_values={"device_id": "pf400"},
    ),
    _RouteSpec(
        method_name="move_defaults_reset",
        invoke=lambda c: c.move_defaults_reset("pf400"),
        operation=ResetMoveDefaultsOperation, path_values={"device_id": "pf400"},
    ),
    _RouteSpec(
        method_name="teachpoints_list",
        invoke=lambda c: list(c.teachpoints_list("dev-1")),
        operation=ListTeachpointsOperation,
    ),
    _RouteSpec(
        method_name="teachpoints_list_all",
        invoke=lambda c: list(c.teachpoints_list_all()),
        operation=ListTeachpointsOperation,
    ),
    _RouteSpec(
        method_name="teachpoints_get",
        invoke=lambda c: c.teachpoints_get("dev-1", "p1"),
        operation=GetTeachpointOperation, path_values={"device_id": "dev-1", "name": "p1"},
    ),
    _RouteSpec(
        method_name="teachpoints_update",
        invoke=lambda c: c.teachpoints_update(
            "dev-1", "p1", _SAMPLE_CARTESIAN_COORDS,
            "ac1", None, "right", True, False, True,
        ),
        operation=UpdateTeachpointOperation, path_values={"device_id": "dev-1", "name": "p1"},
    ),
    _RouteSpec(
        method_name="teachpoints_delete",
        invoke=lambda c: c.teachpoints_delete("dev-1", "p1"),
        operation=DeleteTeachpointOperation, path_values={"device_id": "dev-1", "name": "p1"},
    ),
    _RouteSpec(
        method_name="teachpoints_create",
        invoke=lambda c: c.teachpoints_create("dev-1", [{"position_id": "p1"}]),
        operation=CreateTeachpointsOperation, path_values={"device_id": "dev-1"},
    ),
    _RouteSpec(
        method_name="teachpoints_set_labware_override",
        invoke=lambda c: c.teachpoints_set_labware_override(
            "dev-1", "p1", "costar_96", _SAMPLE_GRIP_PROFILE_PATCH,
        ),
        operation=SetLabwareOverrideOperation, path_values={"device_id": "dev-1", "name": "p1", "labware_type": "costar_96"},
    ),
    _RouteSpec(
        method_name="teachpoints_clear_labware_override",
        invoke=lambda c: c.teachpoints_clear_labware_override(
            "dev-1", "p1", "costar_96",
        ),
        operation=ClearLabwareOverrideOperation,
        path_values={"device_id": "dev-1", "name": "p1", "labware_type": "costar_96"},
    ),
    _RouteSpec(
        method_name="deck_layouts_list",
        invoke=lambda c: list(c.deck_layouts_list("dev-1")),
        operation=ListDeckLayoutsOperation,
    ),
    _RouteSpec(
        method_name="deck_layouts_get",
        invoke=lambda c: c.deck_layouts_get("dev-1", "main"),
        operation=GetDeckLayoutOperation, path_values={"device_id": "dev-1", "name": "main"},
    ),
    _RouteSpec(
        method_name="deck_layouts_save",
        invoke=lambda c: c.deck_layouts_save("dev-1", "main", _SAMPLE_DECK_CONFIG),
        operation=UpdateDeckLayoutOperation, path_values={"device_id": "dev-1", "name": "main"},
    ),
    _RouteSpec(
        method_name="deck_layouts_delete",
        invoke=lambda c: c.deck_layouts_delete("dev-1", "main"),
        operation=DeleteDeckLayoutOperation, path_values={"device_id": "dev-1", "name": "main"},
    ),
    _RouteSpec(
        method_name="reservations_list",
        invoke=lambda c: list(c.reservations_list()),
        operation=ListReservationsOperation,
    ),
    _RouteSpec(
        method_name="reservation_cancel",
        invoke=lambda c: c.reservation_cancel("exec-1", "rsv-1", reason="cleanup"),
        operation=CancelReservationOperation, path_values={"execution_id": "exec-1", "reservation_id": "rsv-1"},
    ),
    _RouteSpec(
        method_name="plugins_list",
        invoke=lambda c: list(c.plugins_list()),
        operation=ListPluginsOperation,
    ),
    _RouteSpec(
        method_name="get_labware",
        invoke=lambda c: c.get_labware("lt1"),
        operation=GetLabwareEntryOperation, path_values={"labware_type": "lt1"},
    ),
    _RouteSpec(
        method_name="add_labware",
        invoke=lambda c: c.add_labware(
            labware_type="lt1", display_name="X", category="plate", geometry={},
        ),
        operation=AddLabwareEntryOperation,
    ),
    _RouteSpec(
        method_name="update_labware",
        invoke=lambda c: c.update_labware(
            "lt1", display_name="X", category="plate", geometry={},
        ),
        operation=UpdateLabwareEntryOperation, path_values={"labware_type": "lt1"},
    ),
    _RouteSpec(
        method_name="delete_labware",
        invoke=lambda c: c.delete_labware("lt1"),
        operation=DeleteLabwareEntryOperation, path_values={"labware_type": "lt1"},
    ),
    _RouteSpec(
        method_name="list_labware",
        invoke=lambda c: list(c.list_labware()),
        operation=GetLabwareCatalogOperation,
    ),
    _RouteSpec(
        method_name="device_initialize",
        invoke=lambda c: c.device_initialize("shk1"),
        operation=InitializeDeviceOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_connect",
        invoke=lambda c: c.device_connect("shk1"),
        operation=ConnectDeviceOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_operator_command",
        invoke=lambda c: c.device_operator_command(MOVE_TO, MoveToRequest(device_id="arm1", position_id="hotel_1")),
        operation=TransporterMoveToOperation, path_values={"device_id": "arm1"},
    ),
    _RouteSpec(
        method_name="device_set_mode",
        invoke=lambda c: c.device_set_mode("shk1", DeviceMode.LIVE),
        operation=SetDeviceModeOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_setup_get",
        invoke=lambda c: c.device_setup_get("shk1"),
        operation=GetDeviceSetupOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_setup_put",
        invoke=lambda c: c.device_setup_put("shk1", DeviceSetup()),
        operation=PutDeviceSetupOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_disconnect",
        invoke=lambda c: c.device_disconnect("shk1"),
        operation=DisconnectDeviceOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_reconcile_deck",
        invoke=lambda c: c.device_reconcile_deck("lh1"),
        operation=ReconcileDeckOperation, path_values={"device_id": "lh1"},
    ),
    _RouteSpec(
        method_name="device_compare_deck",
        invoke=lambda c: c.device_compare_deck("lh1"),
        operation=CompareDeckOperation, path_values={"device_id": "lh1"},
    ),
    _RouteSpec(
        method_name="device_take_control",
        invoke=lambda c: c.device_take_control("shk1", reason="calibrating"),
        operation=TakeDeviceControlOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_release_control",
        invoke=lambda c: c.device_release_control("shk1"),
        operation=ReleaseDeviceControlOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_clear_fault",
        invoke=lambda c: c.device_clear_fault("shk1"),
        operation=ClearDeviceFaultOperation, path_values={"device_id": "shk1"},
    ),
    _RouteSpec(
        method_name="device_get_mounted_tips",
        invoke=lambda c: c.device_get_mounted_tips("lh1"),
        operation=GetMountedTipsOperation, path_values={"device_id": "lh1"},
    ),
    _RouteSpec(
        method_name="device_set_mounted_tips",
        invoke=lambda c: c.device_set_mounted_tips(
            "lh1",
            [MountedTipDTO(channel=0, tip_rack="tips_1", position="A1")],
            reason="reloaded the head by hand",
        ),
        operation=SetMountedTipsOperation, path_values={"device_id": "lh1"},
    ),
    _RouteSpec(
        method_name="device_confirm_mounted_tips",
        invoke=lambda c: c.device_confirm_mounted_tips(
            "lh1", reason="looked at the head",
        ),
        operation=ConfirmMountedTipsOperation, path_values={"device_id": "lh1"},
    ),
    _RouteSpec(
        method_name="pause_thread",
        invoke=lambda c: c.pause_thread("exec-1", "thr-1", reason="test"),
        operation=PauseOperation,
    ),
    _RouteSpec(
        method_name="resume_thread",
        invoke=lambda c: c.resume_thread("exec-1", "thr-1", reason="test"),
        operation=ResumeOperation,
    ),
    _RouteSpec(
        method_name="recover_thread",
        invoke=lambda c: c.recover_thread("exec-1", "thr-1", "RETRY"),
        operation=RecoverThreadOperation, path_values={"execution_id": "exec-1", "thread_id": "thr-1"},
    ),
    _RouteSpec(
        method_name="spawn_thread",
        invoke=lambda c: c.spawn_thread("exec-1", "sample_plate"),
        operation=SpawnThreadOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="labware_list",
        invoke=lambda c: list(c.labware_list()),
        operation=ListLabwareOperation,
    ),
    _RouteSpec(
        method_name="labware_get_by_id",
        invoke=lambda c: c.labware_get_by_id("lw-1"),
        operation=GetLabwareByIdOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_get_by_id_or_none",
        invoke=lambda c: c.labware_get_by_id_or_none("lw-1"),
        operation=GetLabwareByIdOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_get_by_barcode",
        invoke=lambda c: c.labware_get_by_barcode("BC1"),
        operation=GetLabwareByBarcodeOperation, path_values={"barcode": "BC1"},
    ),
    _RouteSpec(
        method_name="labware_get_by_barcode_or_none",
        invoke=lambda c: c.labware_get_by_barcode_or_none("BC1"),
        operation=GetLabwareByBarcodeOperation, path_values={"barcode": "BC1"},
    ),
    _RouteSpec(
        method_name="labware_edit_location",
        invoke=lambda c: c.labware_edit_location("lw-1", "pad_1", "test"),
        operation=EditLabwareLocationOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_edit_barcode",
        invoke=lambda c: c.labware_edit_barcode("lw-1", "BC2"),
        operation=EditLabwareBarcodeOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_set_carry_override",
        invoke=lambda c: c.labware_set_carry_override(
            "lw-1", MoveParameterPatch(speed=30.0),
        ),
        operation=SetLabwareCarryOverrideOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_clear_carry_override",
        invoke=lambda c: c.labware_clear_carry_override("lw-1"),
        operation=ClearLabwareCarryOverrideOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_reset_location",
        invoke=lambda c: c.labware_reset_location("lw-1", "pad_1", "test"),
        operation=ResetLabwareLocationOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_register",
        invoke=lambda c: c.labware_register("plate_tmpl"),
        operation=RegisterLabwareOperation,
    ),
    _RouteSpec(
        method_name="check_run_labware",
        invoke=lambda c: c.check_run_labware("exec-1"),
        operation=GetCheckRunLabwareOperation, path_values={"execution_id": "exec-1"},
    ),
    _RouteSpec(
        method_name="labware_set_well_volumes",
        invoke=lambda c: c.labware_set_well_volumes("lw-1", {"A1": 10.0}, "test"),
        operation=SetWellVolumesOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_resolve_contents",
        invoke=lambda c: c.labware_resolve_contents("lw-1"),
        operation=ResolveContentsOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_mark_tips_used",
        invoke=lambda c: c.labware_mark_tips_used("lw-1", ["A1"], "test"),
        operation=MarkTipsUsedOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_set_tip_state",
        invoke=lambda c: c.labware_set_tip_state("lw-1", ["A1"], "test"),
        operation=SetTipStateOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_confirm_tip_state",
        invoke=lambda c: c.labware_confirm_tip_state("lw-1"),
        operation=ConfirmTipStateOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_confirm_well_volumes",
        invoke=lambda c: c.labware_confirm_well_volumes("lw-1"),
        operation=ConfirmWellVolumesOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="submission_submit",
        invoke=lambda c: c.submission_submit(_sample_submission_request()),
        operation=SubmitExecutionOperation,
    ),
    _RouteSpec(
        method_name="submissions_list",
        invoke=lambda c: list(c.submissions_list()),
        operation=ListSubmissionsOperation,
    ),
    _RouteSpec(
        method_name="submission_get",
        invoke=lambda c: c.submission_get("sub-1"),
        operation=GetSubmissionOperation, path_values={"submission_id": "sub-1"},
    ),
    _RouteSpec(
        method_name="list_thread_templates",
        invoke=lambda c: list(c.list_thread_templates()),
        operation=ListThreadTemplatesOperation,
    ),
    _RouteSpec(
        method_name="list_locations",
        invoke=lambda c: list(c.list_locations()),
        operation=GetTopologyOperation,
    ),
    _RouteSpec(
        method_name="system_info",
        invoke=lambda c: c.system_info(),
        operation=GetSystemInfoOperation,
    ),
    _RouteSpec(
        method_name="methods_list",
        invoke=lambda c: list(c.methods_list()),
        operation=ListMethodsOperation,
    ),
    _RouteSpec(
        method_name="method_get",
        invoke=lambda c: c.method_get("m1"),
        operation=GetMethodOperation, path_values={"name": "m1"},
    ),
    _RouteSpec(
        method_name="workflow_get",
        invoke=lambda c: c.workflow_get("smc"),
        operation=GetWorkflowOperation, path_values={"name": "smc"},
    ),
    _RouteSpec(
        method_name="topology_get",
        invoke=lambda c: c.topology_get(),
        operation=GetTopologyOperation,
    ),
    _RouteSpec(
        method_name="runtime_status",
        invoke=lambda c: c.runtime_status(),
        operation=GetRuntimeStatusOperation,
    ),
    _RouteSpec(
        method_name="runtime_reload",
        invoke=lambda c: c.runtime_reload("edited topology"),
        operation=ReloadRuntimeOperation,
    ),
    _RouteSpec(
        method_name="ops_history_search",
        invoke=lambda c: c.ops_history_search(execution_id="exec-1"),
        operation=SearchOpsHistoryOperation,
    ),
    _RouteSpec(
        method_name="labware_journey",
        invoke=lambda c: c.labware_journey("lw-1"),
        operation=GetLabwareJourneyOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_clear_submission",
        invoke=lambda c: c.labware_clear_submission("sub-1"),
        operation=ClearSubmissionLabwareOperation, path_values={"submission_id": "sub-1"},
    ),
    _RouteSpec(
        method_name="labware_load_run",
        invoke=lambda c: c.labware_load_run(LoadRunLabwareRequest(workflow_name="smc")),
        operation=LoadRunLabwareOperation, path_values={"workflow_name": "smc"},
    ),
    _RouteSpec(
        method_name="labware_discharge",
        invoke=lambda c: c.labware_discharge("lw-1"),
        operation=DischargeLabwareOperation, path_values={"labware_id": "lw-1"},
    ),
    _RouteSpec(
        method_name="labware_clear_all",
        invoke=lambda c: c.labware_clear_all(),
        operation=ClearAllLabwareOperation,
    ),
    _RouteSpec(
        method_name="state_unsettled",
        invoke=lambda c: c.state_unsettled(),
        operation=UnsettledStateOperation,
    ),
    _RouteSpec(
        method_name="recoverable_timeout_extend",
        invoke=lambda c: c.recoverable_timeout_extend("inc-1", 30.0),
        operation=ExtendRecoverableTimeoutOperation, path_values={"incident_id": "inc-1"},
    ),
    _RouteSpec(
        method_name="recoverable_timeout_abort",
        invoke=lambda c: c.recoverable_timeout_abort("inc-1", "op", "why"),
        operation=AbortRecoverableTimeoutOperation, path_values={"incident_id": "inc-1"},
    ),
    _RouteSpec(
        method_name="recoverable_timeout_mark_complete",
        invoke=lambda c: c.recoverable_timeout_mark_complete("inc-1", "op", "why"),
        operation=MarkRecoverableTimeoutCompleteOperation, path_values={"incident_id": "inc-1"},
    ),
)


def _spec_id(spec: _RouteSpec) -> str:
    return spec.method_name


def _record_handler(
    recorder: list[tuple[str, str]],
    response_body: object = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Capture (method, path) of every request and respond with stub data.

    The stub uses a single recorded payload for every route since this guard
    only checks the wire address, not the response semantics. Per-route DTOs
    are validated by the dedicated per-route tests (e.g. routes' own unit
    tests + the DTO tests above).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append((request.method, request.url.path))
        return httpx.Response(200, json=response_body if response_body is not None else {})

    return handler


# The blocker list the server merges into an operation's answer, empty.
_NO_BLOCKERS: dict[str, JsonValue] = {
    "blockers": [], "blocker_count": 0, "blockers_fingerprint": "none",
}

# Each method's response shape that the client-side parser tolerates. Empty
# objects / lists keep the parser happy across the catalog.
_OK_RESPONSE_BY_METHOD: dict[str, object] = {
    "health": {"status": "ok"},
    # POST /executions answers SubmitExecutionResponse on both backends.
    # ``id`` is the SUBMISSION id; ``execution_id`` is the EXECUTION id.
    "submit_workflow": {
        "id": "sub-1",
        "execution_id": "exec-1",
        "workflow_name": "smc",
        "group_count": 1,
        "status": "running",
        "batch_mode": "STANDALONE",
        "submitted_at": "2026-05-12T00:00:00Z",
        "run_mode": "CHECK_RUN",
        "operator_id": None,
        "deployment_profile": None,
    },
    "list_executions": {"executions": []},
    "get_execution": {"id": "exec-1", "workflow_name": "smc", "status": "running"},
    "stop_execution": {
        **_NO_BLOCKERS,
        "status": "armed", "execution_id": "exec-1", "phase": "accepting",
        "message": "execution paused and abort armed",
    },
    "set_execution_sim_speedup": {
        "execution_id": "exec-1", "sim_speedup": 0.5,
        "message": "2x faster than real time",
    },
    "execution_remove": {"status": "removed", "execution_id": "exec-1"},
    "get_thread_detail": {
        "id": "thr-1",
        "name": "journey",
        "status": "EXECUTING_ACTION",
        "current_location": "incubator",
        "current_method": None,
        "completed_method_count": 0,
        "last_error": None,
        "pause_reason": None,
        "completed_methods": [],
    },
    "device_introspection": {
        "name": "shk1",
        "type": "Shaker",
        "interfaces": [],
        "capabilities": [],
        "provides_state": False,
        "methods": {},
        "danger_levels": {},
    },
    # Daemon wraps in {"devices": [...]}; a hosted server wraps the same way. The
    # control-plane DTO unwraps to a flat list per backend client. An
    # empty wrapper here exercises the unwrap path on both.
    "device_registry_list": {"devices": []},
    "device_registry_show": {
        "name": "shk1",
        "topology_card": None,
        "connection_card": None,
        "is_client_connected": False,
        "is_device_connected": False,
        "is_initialized": False,
        "device_link_mode": None,
        "mode_eligibility": {
            "check_run": False, "sim": False, "live": False,
        },
    },
    "thread_skip_method": {**_NO_BLOCKERS, "status": "mutated", "execution_id": "exec-1", "thread_id": "thr-1"},
    "thread_insert_method": {**_NO_BLOCKERS, "status": "mutated", "execution_id": "exec-1", "thread_id": "thr-1"},
    "thread_skip_action": {**_NO_BLOCKERS, "status": "mutated", "execution_id": "exec-1", "thread_id": "thr-1"},
    "thread_insert_action": {**_NO_BLOCKERS, "status": "mutated", "execution_id": "exec-1", "thread_id": "thr-1"},
    "thread_replace_method": {"execution_id": "exec-1", "thread_id": "thr-1", **_NO_BLOCKERS},
    "thread_replace_action": {"execution_id": "exec-1", "thread_id": "thr-1", **_NO_BLOCKERS},
    "audit_list": [],
    "events_list": [],
    "intervention_next": {"intervention": None, "next_cursor": 3},
    "submit_method": {"execution_id": "exec-2", "status": "running", "workflow_name": "standalone-method:hello.incubate:abcd1234", "status": "running"},
    "variables_list": {},
    "variables_get_global": {"name": "n", "value": 0, "source": "global"},
    "variables_unset_global": {"status": "unset", "existed": True},
    "variables_get": {
        "name": "n", "execution_id": "exec-1", "value": 0,
        "source": "execution", "overrides": [],
    },
    "variables_set": {"name": "n", "value": 0, "shadowed_by": []},
    "variables_set_submission": {"name": "n", "value": 0, "shadowed_by": []},
    "variables_set_global": {"name": "n", "value": 0},
    "variables_unset": {"status": "unset", "existed": True},
    "variables_unset_submission": {"status": "unset", "existed": True},
    "variables_list_all": [],
    "access_configs_list": [],
    "profiles_list": [],
    "profiles_get": {"name": "qc"},
    "access_configs_get": {
        "name": "ac1", "access_type": "vertical", "gripper_offset": 20.0,
        "vertical_clearance": 20.0, "horizontal_clearance": 100.0,
    },
    "teachpoints_list": [],
    "teachpoints_list_all": [],
    "teachpoints_get": {
        "device_id": "dev-1", "position_id": "p1", "coord_type": "cartesian",
        "coords": {}, "access_config_name": None, "gateway": None,
        "orientation": None,
    },
    "teachpoints_update": {
        "device_id": "dev-1", "position_id": "p1", "coord_type": "cartesian",
        "coords": {"type": "cartesian", "x": 1.0, "y": 2.0, "z": 3.0,
                   "yaw": 0.0, "pitch": 90.0, "roll": 180.0},
        "access_config_name": "ac1", "gateway": None, "orientation": "right",
    },
    "teachpoints_set_labware_override": {
        "device_id": "dev-1", "position_id": "p1", "coord_type": "cartesian",
        "coords": {"type": "cartesian", "x": 1.0, "y": 2.0, "z": 3.0,
                   "yaw": 0.0, "pitch": 90.0, "roll": 180.0},
        "access_config_name": "ac1", "gateway": None, "orientation": "right",
        "by_labware": {"costar_96": {"resource_width": 76.0}},
    },
    "teachpoints_clear_labware_override": {},
    "teachpoints_delete": {},
    "teachpoints_create": {
        "total": 1, "created": 1, "updated": 0,
        "summary": "saved 1 teachpoints (1 created, 0 updated)",
    },
    "deck_layouts_list": [],
    "deck_layouts_get": {
        "device_id": "dev-1", "name": "main",
        "config": {"deck_type": "OT2", "resources": []},
    },
    "reservations_list": [],
    "reservation_cancel": {**_NO_BLOCKERS, "status": "cancelled"},
    "plugins_list": [],
    "get_labware": {
        "labware_type": "lt1", "display_name": "X", "category": "plate",
        "vendor": None, "source": "operator_custom", "geometry": {},
        "plr_class_name": None,
    },
    "add_labware": {
        "labware_type": "lt1", "display_name": "X", "category": "plate",
        "vendor": None, "source": "operator_custom", "geometry": {},
        "plr_class_name": None,
    },
    "update_labware": {
        "labware_type": "lt1", "display_name": "X", "category": "plate",
        "vendor": None, "source": "operator_custom", "geometry": {},
        "plr_class_name": None,
    },
    "delete_labware": {},
    "list_labware": {"labware": []},
    "device_initialize": {"status": "initialized", "device_name": "shk1"},
    "device_connect": {"status": "connected", "device_name": "shk1"},
    "device_disconnect": {"status": "disconnected", "device_name": "shk1"},
    "device_set_mode": {"device_name": "shk1", "mode": "LIVE", "switched": True, "warning": None},
    "device_setup_get": {"device_name": "shk1", "setup": {}},
    "device_setup_put": {"device_name": "shk1", "setup": {}},
    "device_operator_command": {"success": True, "message": "moved arm1 to hotel_1", "result": {}},
    "device_reconcile_deck": {"status": "reconciled", "device_name": "lh1", **_NO_BLOCKERS},
    "device_compare_deck": {
        "device_name": "lh1", "agrees": True, "driver_deck_empty": False,
        **_NO_BLOCKERS,
    },
    "device_take_control": {"status": "held", "device_name": "shk1"},
    "device_release_control": {**_NO_BLOCKERS, "status": "released", "device_name": "shk1"},
    "device_clear_fault": {
        "status": "no_fault", "device_name": "shk1", "cleared": None, **_NO_BLOCKERS,
    },
    "device_get_mounted_tips": {
        "device_name": "lh1", "mounted": [], "provenance": "unknown",
    },
    "device_set_mounted_tips": {"device_name": "lh1"},
    "device_confirm_mounted_tips": {"device_name": "lh1"},
    "pause_thread": {**_NO_BLOCKERS, "result": {
        "kind": "thread", "execution_id": "exec-1", "thread_id": "thr-1", "status": "pausing",
    }},
    "resume_thread": {**_NO_BLOCKERS, "result": {
        "kind": "thread", "execution_id": "exec-1", "thread_id": "thr-1", "status": "resumed",
    }},
    "recover_thread": {**_NO_BLOCKERS, "status": "recovered", "execution_id": "exec-1", "thread_id": "thr-1"},
    "spawn_thread": {
        "id": "thr-1", "name": "sample_plate", "status": "running",
        "current_location": "pad_1", "current_method": None,
        "completed_method_count": 0, "last_error": None,
        "pause_reason": None, "completed_methods": [],
    },
    "labware_list": {"labware": []},
    "labware_get_by_id": {
        "labware": {
            "id": "lw-1", "name": "plate", "template_name": "plate_tmpl",
            "labware_type": "Cor_96_wellplate_360ul_Fb",
            "barcode": None, "current_location": None,
        },
    },
    "labware_get_by_id_or_none": {
        "labware": {
            "id": "lw-1", "name": "plate", "template_name": "plate_tmpl",
            "labware_type": "Cor_96_wellplate_360ul_Fb",
            "barcode": None, "current_location": None,
        },
    },
    "labware_get_by_barcode": {
        "labware": {
            "id": "lw-1", "name": "plate", "template_name": "plate_tmpl",
            "labware_type": "Cor_96_wellplate_360ul_Fb",
            "barcode": "BC1", "current_location": None,
        },
    },
    "labware_get_by_barcode_or_none": {
        "labware": {
            "id": "lw-1", "name": "plate", "template_name": "plate_tmpl",
            "labware_type": "Cor_96_wellplate_360ul_Fb",
            "barcode": "BC1", "current_location": None,
        },
    },
    "labware_edit_location": {**_NO_BLOCKERS, "status": "ok", "labware_id": "lw-1"},
    "labware_edit_barcode": {"status": "ok", "labware_id": "lw-1"},
    "labware_reset_location": {"status": "ok", "labware_id": "lw-1"},
    "labware_set_carry_override": {
        "labware_id": "lw-1", "carry_override": {"speed": 30.0},
    },
    "labware_clear_carry_override": {
        "labware_id": "lw-1", "carry_override": {},
    },
    "labware_register": {
        "labware": {
            "id": "lw-1", "name": "plate", "template_name": "plate_tmpl",
            "labware_type": "Cor_96_wellplate_360ul_Fb",
            "barcode": None, "current_location": None,
        },
        "status": "registered",
        **_NO_BLOCKERS,
    },
    "check_run_labware": {"execution_id": "exec-1", "labware": []},
    "labware_set_well_volumes": {**_NO_BLOCKERS, "status": "ok", "labware_id": "lw-1"},
    "labware_resolve_contents": {
        "labware_id": "lw-1", "labware_name": "tips-abcd1234",
        "provenance": "known", "source": "declaration",
        "tip_positions_present": ["A1"], "tip_count": 1,
        "volumes": None,
        "layers": [
            {"layer": "ledger", "tip_count": 1, "agrees": True, "note": "the record"},
        ],
    },
    "labware_mark_tips_used": {
        "labware_id": "lw-1", "tip_positions_present": ["A2"], **_NO_BLOCKERS,
    },
    "labware_set_tip_state": {**_NO_BLOCKERS, "status": "ok", "labware_id": "lw-1"},
    "labware_confirm_tip_state": {**_NO_BLOCKERS, "status": "ok", "labware_id": "lw-1"},
    "labware_confirm_well_volumes": {"status": "ok", "labware_id": "lw-1"},
    "submission_submit": {
        "id": "sub-1", "execution_id": "exec-1", "workflow_name": "smc",
        "group_count": 1, "status": "ACCEPTED", "batch_mode": "STANDALONE",
        "submitted_at": "2026-05-12T00:00:00Z", "run_mode": "CHECK_RUN",
        "operator_id": None, "deployment_profile": None,
    },
    "submissions_list": {"submissions": []},
    "submission_get": {
        "submission": {
            "id": "sub-1", "execution_id": "exec-1", "workflow_name": "smc",
            "group_count": 1, "status": "ACCEPTED", "batch_mode": "STANDALONE",
            "submitted_at": "2026-05-12T00:00:00Z", "run_mode": "CHECK_RUN",
            "operator_id": None, "deployment_profile": None,
        },
    },
    "list_thread_templates": {"threads": []},
    "list_locations": {"devices": [], "locations": []},
    "system_info": {
        "name": "test-system", "description": "test", "version": "1.0",
        "device_count": 0, "transporter_count": 0, "workflow_count": 0, "method_count": 0,
    },
    # Daemon wraps in {"methods": [...]}; a hosted server does the same. Empty list
    # satisfies both parsers.
    "methods_list": {"methods": []},
    "method_get": {
        "method": {
            "workflow_name": "smc", "name": "m1", "failure_policy": "ABORT",
        },
    },
    "workflow_get": {
        "workflow": {"name": "smc", "entry_thread_template_names": []},
    },
    # Live composed-topology view: every list optional, empty satisfies both.
    "topology_get": {
        "devices": [], "transporters": [], "resource_pools": [],
        "locations": [], "labware_templates": [],
    },
    "runtime_status": {"built": True, "last_build_error": None},
    "runtime_reload": {
        "head_sha": None, "registered": ["smc"], "removed": [], "unchanged": [],
        **_NO_BLOCKERS,
    },
    "ops_history_search": {"records": []},
    "labware_journey": {"labware_id": "lw-1", "entries": []},
    "labware_clear_submission": {"cleared": [], "preserved_reuse_bound": []},
    "release_mover_hold": {
        **_NO_BLOCKERS,
        "mover_name": "pf400", "labware_id": "lw-1",
        "labware_name": "assay_plate-1", "released_to": "pad_1",
        "discharged": False,
    },
    "labware_discharge": {"labware_id": "lw-1", "status": "discharged", **_NO_BLOCKERS},
    "labware_clear_all": {"cleared": []},
    "labware_load_run": {"cleared": [], "loaded": [], "kept": [], "not_loaded": [], **_NO_BLOCKERS},
    "execution_close": {"execution_id": "exec-1", "phase": "draining"},
    "pause_all_threads": {**_NO_BLOCKERS, "result": {
        "kind": "execution", "pausing": 0, "already_paused": 0, "terminal_skipped": 0,
    }},
    "resume_all_threads": {**_NO_BLOCKERS, "result": {
        "kind": "execution", "resumed": 0, "pause_cancelled": 0, "error_skipped": 0, "completed_skipped": 0,
    }},
    "manual_steps_list": {"pending": []},
    "manual_step_confirm": {**_NO_BLOCKERS, "status": "confirmed", "execution_id": "exec-1", "step_id": "step-1"},
    "incidents_list": [],
    "incidents_get": {
        "id": "inc-1", "timestamp": 1700000000.0, "category": "device",
        "severity": "error", "execution_id": None, "thread_id": None,
        "message": "m", "detail": {}, "recovery_action": "none", "acknowledged": False,
    },
    "incidents_ack": {"acknowledged_count": 1},
    "incidents_ack_all": {"acknowledged_count": 0},
    "list_workflows": {"workflows": []},
    "state_unsettled": {"unsettled": []},
    "access_configs_add": {
        "name": "ac1", "access_type": "vertical", "gripper_offset": 20.0,
        "vertical_clearance": 20.0, "horizontal_clearance": 100.0,
    },
    "access_configs_update": {
        "name": "ac1", "access_type": "vertical", "gripper_offset": 20.0,
        "vertical_clearance": 20.0, "horizontal_clearance": 100.0,
    },
    "access_configs_delete": {},
    "profiles_add": {"name": "qc"},
    "profiles_update": {"name": "qc"},
    "profiles_delete": {},
    "grip_profiles_list": [],
    "grip_profiles_get": _SAMPLE_GRIP_PROFILE_RECORD,
    "grip_profiles_patch": _SAMPLE_GRIP_PROFILE_RECORD,
    "grip_profiles_reset": {},
    "move_defaults_list": [],
    "move_defaults_get": _SAMPLE_MOVE_DEFAULTS_RECORD,
    "move_defaults_patch": _SAMPLE_MOVE_DEFAULTS_RECORD,
    "move_defaults_reset": {},
    "deck_layouts_save": {
        "device_id": "dev-1", "name": "main",
        "config": {"deck_type": "BRAVO_96", "resources": []},
    },
    "deck_layouts_delete": {},
    "recoverable_timeout_extend": {"incident_id": "inc-1", "decision": "extend", **_NO_BLOCKERS},
    "recoverable_timeout_abort": {"incident_id": "inc-1", "decision": "abort", **_NO_BLOCKERS},
    "recoverable_timeout_mark_complete": {
        "incident_id": "inc-1", "decision": "mark_complete", **_NO_BLOCKERS,
    },
}


def _hosted_exchange(
    call: Callable[[OrcaClient], dict[str, list[str]]],
    response_body: object,
) -> tuple[httpx.Request, dict[str, list[str]]]:
    """Run one call against a hosted server and hand back the request it sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=response_body)

    result = call(_wire_cloud(handler))
    assert sent, "no HTTP request was made"
    return sent[0], result


def test_hosted_clear_verbs_send_the_operation_body_a_hosted_server_parses() -> None:
    """The three clear verbs name what they clear in the path and carry the rest in the JSON body.

    A hosted server binds them as operations, whose requests are whole models; sending
    a field as a query parameter leaves the body missing it.
    """
    request, _ = _hosted_exchange(
        lambda c: c.labware_clear_submission("sub-1", force=True),
        {"cleared": [], "preserved_reuse_bound": []},
    )
    assert request.url.path == "/api/submissions/sub-1/clear-labware"
    assert request.url.query == b""
    assert json.loads(request.content) == {"force": True}

    request, _ = _hosted_exchange(
        lambda c: c.labware_discharge("lw-1"),
        {"labware_id": "lw-1", "status": "discharged", **_NO_BLOCKERS},
    )
    assert request.url.path == "/api/labware-instances/lw-1/discharge"
    assert request.url.query == b""
    assert json.loads(request.content) == {"force": False}

    request, _ = _hosted_exchange(lambda c: c.labware_clear_all(force=True), {"cleared": []})
    assert request.url.query == b""
    assert json.loads(request.content) == {"force": True}


def test_hosted_clear_verbs_read_the_hosted_servers_own_answer() -> None:
    """A hosted server answers in the operation models' shapes, which forbid extras.

    The CLI contract is the same on either backend, so what a hosted server names has to
    reach the caller as the id list the daemon backend also returns.
    """
    _, discharged = _hosted_exchange(
        lambda c: c.labware_discharge("lw-1"),
        {"labware_id": "lw-1", "status": "discharged", **_NO_BLOCKERS},
    )
    assert discharged == {"cleared_labware_ids": ["lw-1"]}

    _, cleared = _hosted_exchange(
        lambda c: c.labware_clear_all(), {"cleared": ["lw-1", "lw-2"]},
    )
    assert cleared == {"cleared_labware_ids": ["lw-1", "lw-2"]}

    _, submission = _hosted_exchange(
        lambda c: c.labware_clear_submission("sub-1"),
        {"cleared": ["lw-1"], "preserved_reuse_bound": ["reagent-1"]},
    )
    assert submission == {"cleared": ["lw-1"], "preserved_reuse_bound": ["reagent-1"]}


@pytest.mark.parametrize("spec", ROUTE_CATALOG, ids=_spec_id)
def test_each_call_sends_its_catalog_path(spec: _RouteSpec) -> None:
    recorder: list[tuple[str, str]] = []
    body = _OK_RESPONSE_BY_METHOD[spec.method_name]
    client = _wire_cloud(_record_handler(recorder, body))
    spec.invoke(client)
    assert recorder, f"{spec.method_name}: no HTTP request was made"
    method, path = recorder[0]
    expected_method, expected_path = spec.expected_route()
    assert (method, path) == (expected_method, expected_path), (
        f"{spec.method_name} hit {method} {path}, its Operation is served at {expected_method} {expected_path}"
    )


def _recorded_exchange(spec: _RouteSpec) -> httpx.Request:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_OK_RESPONSE_BY_METHOD[spec.method_name])

    spec.invoke(_wire_cloud(handler))
    assert sent, f"{spec.method_name}: no HTTP request was made"
    return sent[0]


_CATALOG_SPECS = [spec for spec in ROUTE_CATALOG if spec.operation is not None]


@pytest.mark.parametrize("spec", _CATALOG_SPECS, ids=_spec_id)
def test_each_call_sends_a_request_its_operation_accepts(spec: _RouteSpec) -> None:
    """The query or body, with the path values added back, is a whole Request of the Operation the call reaches."""
    operation = spec.operation
    assert operation is not None
    request = _recorded_exchange(spec)
    binding = operation.binding
    sent: dict[str, object] = (
        dict(request.url.params) if binding.fields_in_query
        else (json.loads(request.content) if request.content else {})
    )
    assert not set(binding.path_params) & set(sent), "a path parameter was also sent as a query or body field"
    operation.Request.model_validate({**sent, **spec.path_values})


def test_every_operation_the_catalog_names_is_served_by_the_daemon() -> None:
    served = {entry.op_class for entry in DaemonOperationCatalog().entries()}
    named = {spec.operation for spec in _CATALOG_SPECS}
    assert named <= served


def _route_literals(source: str, prefixes: set[str]) -> list[str]:
    """The route prefixes typed in `source`, bare (`"/audit"`) or under the API prefix (`"/api/audit"`)."""
    return [
        prefix for prefix in sorted(prefixes)
        if re.search(rf"""["'`](?:{re.escape(API_PREFIX)})?{re.escape(prefix)}(?![\w-])""", source)
    ]


# Plug-in backends live here and declare their own routes, so their paths are checked beside them.
_BACKENDS_DIRECTORY = "backends"

_TYPED_API_PATH = re.compile(r"""["'`]/api/\S*""")


def typed_api_paths(source: str) -> list[str]:
    """Every quoted `/api/...` literal in `source`."""
    return _TYPED_API_PATH.findall(source)


def _files_that_type_a_route(root: Path, prefixes: set[str]) -> dict[str, list[str]]:
    return {
        source.relative_to(root).as_posix(): hits
        for source in sorted(root.rglob("*.py"))
        if source.relative_to(root).parts[0] != _BACKENDS_DIRECTORY
        if (hits := _route_literals(source.read_text(encoding="utf-8"), prefixes))
    }


def test_the_route_literal_scan_finds_a_typed_path_and_ignores_a_longer_word() -> None:
    assert _route_literals('self.get("/audit", params={})', {"/audit"}) == ["/audit"]
    assert _route_literals('title = "/auditing"', {"/audit"}) == []


def test_the_route_literal_scan_finds_a_path_typed_with_its_api_prefix() -> None:
    assert _route_literals('c.post("/api/audit", json={})', {"/audit"}) == ["/audit"]


def test_the_route_scan_reads_a_file_in_a_subfolder(tmp_path: Path) -> None:
    (tmp_path / "verbs").mkdir()
    (tmp_path / "verbs" / "audit.py").write_text('c.post("/api/audit")', encoding="utf-8")
    (tmp_path / "clean.py").write_text("x = 1", encoding="utf-8")
    assert _files_that_type_a_route(tmp_path, {"/audit"}) == {"verbs/audit.py": ["/audit"]}


def test_the_api_path_scan_finds_a_typed_path() -> None:
    assert typed_api_paths('c.put("/api/things/{name}", json={})') == ['"/api/things/{name}",']
    assert typed_api_paths("x = 1  # POST api things") == []


def test_the_cli_types_no_path_an_operation_is_served_at() -> None:
    """Every Operation route is read from its binding. `/health` and `/shutdown` are outside the catalog."""
    prefixes = {
        entry.op_class.binding.path.split("{")[0].rstrip("/") for entry in DaemonOperationCatalog().entries()
    } - {""}
    assert _files_that_type_a_route(Path(cli_app_module.__file__).parent, prefixes) == {}


def test_route_catalog_covers_every_protocol_method() -> None:
    """A Protocol method with no route catalog entry fails here."""
    catalog_methods = {spec.method_name for spec in ROUTE_CATALOG}
    protocol_methods = {
        name
        for name, value in inspect.getmembers(IControlPlaneClient)
        if not name.startswith("_") and inspect.isfunction(value)
    }
    missing = sorted(protocol_methods - catalog_methods)
    extra = sorted(catalog_methods - protocol_methods)
    assert not missing, (
        f"IControlPlaneClient methods missing from ROUTE_CATALOG: {missing}. "
        f"Add a _RouteSpec for each."
    )
    assert not extra, (
        f"ROUTE_CATALOG entries that don't exist on IControlPlaneClient: "
        f"{extra}. Either remove the entry or add the method."
    )



# -- Read limits ----------------------------------------------------------------
#
# The binding alone decides whether the CLI waits for an answer without a read limit.

_DEFAULT_READ_LIMIT_S = 30.0


def _read_limit(request: httpx.Request) -> float | None:
    return request.extensions["timeout"]["read"]


def _binding_of(spec: _RouteSpec) -> RestBinding:
    assert spec.operation is not None
    return spec.operation.binding


_DEVICE_SPECS = [spec for spec in _CATALOG_SPECS if _binding_of(spec).drives_device]
_TIMED_SPECS = [spec for spec in _CATALOG_SPECS if not _binding_of(spec).never_timed_out]
_LONG_POLL_SPECS = [spec for spec in _CATALOG_SPECS if _binding_of(spec).long_poll]


@pytest.mark.parametrize("spec", _DEVICE_SPECS, ids=_spec_id)
def test_a_route_that_drives_a_device_has_no_read_limit(spec: _RouteSpec) -> None:
    assert _read_limit(_recorded_exchange(spec)) is None


@pytest.mark.parametrize("spec", _LONG_POLL_SPECS, ids=_spec_id)
def test_a_long_poll_route_has_no_read_limit(spec: _RouteSpec) -> None:
    assert _read_limit(_recorded_exchange(spec)) is None


@pytest.mark.parametrize("spec", _TIMED_SPECS, ids=_spec_id)
def test_a_route_with_neither_flag_keeps_the_read_limit(spec: _RouteSpec) -> None:
    assert _read_limit(_recorded_exchange(spec)) == _DEFAULT_READ_LIMIT_S


def test_the_catalog_has_routes_of_each_kind() -> None:
    assert _DEVICE_SPECS and _TIMED_SPECS and _LONG_POLL_SPECS


class _FakeDaemon(DaemonLoadingClient):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__("http://test-server")
        self._handler = handler

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self._handler), base_url=self._base_url, timeout=self._timeout,
        )


def read_limit_of(call: Callable[[Callable[[httpx.Request], httpx.Response]], object], body: JsonValue) -> float | None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    call(handler)
    assert sent
    return _read_limit(sent[0])


@pytest.mark.parametrize(
    ("call", "body"),
    [
        pytest.param(lambda h: _Fake(h).unload(), {}, id="unload"),
        pytest.param(lambda h: _Fake(h).shutdown(), {}, id="shutdown"),
        pytest.param(
            lambda h: _FakeDaemon(h).mount_package(), {"package_dir": "p", "runtime_state": "RUNNING"},
            id="mount_package",
        ),
    ],
)
def test_a_call_outside_the_protocol_whose_route_drives_a_device_has_no_read_limit(
    call: Callable[[Callable[[httpx.Request], httpx.Response]], object], body: JsonValue,
) -> None:
    assert read_limit_of(call, body) is None


# -- CLI verb inventory (fail-closed) -----------------------------------------
#
# The full ``orca`` verb tree, enumerated live from the Typer app. Every verb
# must appear here. Hidden verbs are included: a command exists whether or not
# help shows it.
EXPECTED_CLI_VERBS: frozenset[str] = frozenset({
    'access-configs create',
    'access-configs delete',
    'access-configs list',
    'access-configs show',
    'access-configs update',
    'audit list',
    'events list',
    'deck-layouts delete',
    'deck-layouts list',
    'deck-layouts save',
    'deck-layouts show',
    'describe device',
    'describe location',
    'describe method',
    'describe system',
    'describe thread',
    'describe workflow',
    'device capabilities',
    'device clear-fault',
    'device compare-deck',
    'device connect',
    'device disconnect',
    'device mode',
    'device setup',
    'device write-setup',
    'device aspirate',
    'device batch',
    'device configure-deck',
    'device dispense',
    'device mix',
    'device move-to',
    'device open-gripper',
    'device pick-plate',
    'device place-plate',
    'device confirm-mounted-tips',
    'device mounted-tips',
    'device reconcile-deck',
    'device release-hold',
    'device set-mounted-tips',
    'device release-control',
    'device take-control',
    'device info',
    'device initialize',
    'device list',
    'device registry show',
    'device send',
    'execution close',
    'execution detail',
    'execution list',
    'execution pause',
    'execution remove',
    'execution resume',
    'execution stop',
    'execution sim-speedup',
    'execution thread detail',
    'execution thread insert-action',
    'execution thread insert-method',
    'execution thread pause',
    'execution thread recover',
    'execution thread replace-action',
    'execution thread replace-method',
    'execution thread resume',
    'execution thread skip-action',
    'execution thread skip-method',
    'execution thread spawn',
    'execution threads',
    'incident ack',
    'incident ack-all',
    'incident get',
    'incident list',
    'incident recoverable-timeout abort',
    'incident recoverable-timeout extend',
    'incident recoverable-timeout mark-complete',
    'intervention next',
    'labware add',
    'labware carry',
    'labware carry-normally',
    'labware catalog',
    'labware clear-all',
    'labware clear-submission',
    'labware delete',
    'labware discharge',
    'labware confirm-tips',
    'labware confirm-volumes',
    'labware contents',
    'labware edit-barcode',
    'labware edit-location',
    'labware get',
    'labware check-run',
    'labware load-run',
    'labware journey',
    'labware list',
    'labware mark-tips-used',
    'labware register',
    'labware reset-location',
    'labware set-tips',
    'labware set-volume',
    'labware update',
    'labware where',
    'location list',
    'manual-step confirm',
    'manual-step list',
    'method execute',
    'method get',
    'plugin list',
    'profiles create',
    'profiles delete',
    'profiles list',
    'profiles show',
    'profiles update',
    'grip-profiles list',
    'grip-profiles reset',
    'grip-profiles set',
    'grip-profiles show',
    'method list',
    'move-defaults list',
    'move-defaults reset',
    'move-defaults set',
    'move-defaults show',
    'ops-history get',
    'ops-history search',
    'reservation cancel',
    'reservation list',
    'run',
    'runtime reload',
    'runtime status',
    'shutdown',
    'start',
    'state unsettled',
    'status',
    'submission detail',
    'submission list',
    'submission submit',
    'teachpoints create',
    'teachpoints delete',
    'teachpoints capture',
    'teachpoints export',
    'teachpoints import',
    'teachpoints labware clear',
    'teachpoints labware list',
    'teachpoints labware set',
    'teachpoints list',
    'teachpoints show',
    'teachpoints update',
    'thread list',
    'topology get',
    'topology mount',
    'unload',
    'var get',
    'var get-global',
    'var list',
    'var set',
    'var set-global',
    'var unset',
    'var unset-global',
    'version',
    'workflow get',
    'workflow list',
    'workflow load',
})


def _live_cli_verbs() -> set[str]:
    """Every leaf Typer command as a space-separated verb path.

    Reads Typer's own ``registered_commands`` / ``registered_groups`` rather
    than ``typer.main.get_command(...)``: the click command tree the latter
    builds was empty under CI's click version (its lazy ``.commands`` cache
    never populated), so the guard read no verbs at all. The registration
    structures are populated unconditionally at import, so this is stable.
    """
    from typer.main import get_command_name

    verbs: set[str] = set()

    def _walk(t_app: typer.Typer, prefix: list[str]) -> None:
        for cmd_info in t_app.registered_commands:
            callback = cmd_info.callback
            name = cmd_info.name or (
                get_command_name(callback.__name__) if callback is not None else ""
            )
            if name:
                verbs.add(" ".join(prefix + [name]))
        for group_info in t_app.registered_groups:
            assert group_info.typer_instance is not None
            # Always recurse: a nameless sub-app (add_typer(name=None)) keeps
            # the parent prefix rather than being skipped, so its verbs can't
            # slip past the fail-closed guard.
            name = group_info.name
            _walk(
                group_info.typer_instance,
                prefix + [name] if name else prefix,
            )

    _walk(cli_app, [])
    return verbs


def test_cli_verbs_match_expected() -> None:
    """Fail-closed: the live verb tree must equal EXPECTED_CLI_VERBS."""
    live = _live_cli_verbs()
    added = sorted(live - EXPECTED_CLI_VERBS)
    removed = sorted(EXPECTED_CLI_VERBS - live)
    assert not added, (
        f"New CLI verbs not in EXPECTED_CLI_VERBS: {added}. Add them to the inventory."
    )
    assert not removed, (
        f"EXPECTED_CLI_VERBS lists verbs that no longer exist: {removed}. "
        f"Remove or rename them."
    )
