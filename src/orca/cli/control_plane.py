"""What a CLI verb can call, and the one error a failed call raises.

Every server is reached through `orca.cli.client.OrcaClient`, which returns the
wire models the servers answer with. The hosted-only verbs (loading a workflow
file, submitting the topology) are `ICloudControlPlaneClient`, implemented in
`orca.cli.backends`. This module names only what the verbs read from their answer.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from pydantic import BaseModel, ConfigDict, JsonValue

from orca.modes.device_modes import DeviceMode
from orca.operations.events import PastEvent
from orca.operations.operator_device_models import OperatorRoute
from orca.operations.check_run_models import GetCheckRunLabwareResponse
from orca.operations.submission_models import MethodExecutionResponse
from orca.operations.manual_step_models import PendingManualStepDTO
from orca.operations.interventions import NextInterventionResponse
from orca.operations.state_models import UnsettledStateResponse
from orca.operations.system_models import GetSystemInfoResponse
from orca.operations.run_loading_models import LoadRunLabwareRequest, LoadRunLabwareResponse
from orca.operations.teachpoint_transfer_models import CreateTeachpointsResponse
from orca.operations.submission_models import SubmitExecutionResponse
from orca.variables.deployment_profile import DeploymentProfile
from orca.variables.errors import OptionValue
from orca.operations.wire_models import (
    AccessConfigDTO,
    GripProfileDTO,
    GripProfilePatchRequest,
    IncidentAckResponse,
    IncidentResponse,
    RecoverableTimeoutDecisionResponse,
    MoveDefaultsDTO,
    MoveDefaultsPatchRequest,
    DeckLayoutDTO,
    DeckLayoutSummaryDTO,
    AuditEntryDTO,
    DeviceIntrospectionDTO,
    DeviceRegistryEntryResponse,
    LocationDTO,
    MethodTemplateDTO,
    OptionValueJson,
    PluginDTO,
    ReservationSnapshotDTO,
    RunModeStr,
    ReloadResponse,
    RuntimeStatusResponse,
    SubmissionDTO,
    TeachpointDTO,
    ThreadSnapshotResponse,
    ThreadTemplateDTO,
    TopologySourceResponse,
    TopologyViewResponse,
    WorkflowTemplateDTO,
)
from orca.operations.variable_models import (
    CrossExecVariableDTO,
    VariableSetResponse,
    VariableValue,
)
from orca.operations.submission_models import SubmitExecutionRequest

from orca.operations.device_setup_models import DeviceSetupResponse
from orca.operations.device_models import (
    SetDeviceModeResponse,
    ClearDeviceFaultResponse,
    CompareDeckResponse,
    GetMountedTipsResponse,
    MountedTipDTO,
    ReconcileDeckResponse,
)
from orca.operations.labware_models import (
    GetLabwareJourneyResponse,
    LabwareSnapshotModel,
    MarkTipsUsedResponse,
    ReleaseMoverHoldResponse,
    ResolveContentsResponse,
)
from orca.operations.execution_models import (
    CloseExecutionResponse,
    ExecutionRecordModel,
    GetExecutionDetailResponse,
    SetExecutionSimSpeedupResponse,
    StopExecutionResponse,
)
from orca.operations.labware_catalog_models import LabwareCatalogEntry, LabwareCatalogSummary
from orca.operations.ops_history import SearchOpsHistoryResponse
from orca.operations.thread_models import InsertWhere, ReplaceResult, ResumeExecutionResult


class _CPModel(BaseModel):
    """Base for the response types of the hosted-only verbs."""

    model_config = ConfigDict(extra="ignore")


ENV_CLOUD_API_KEY = "ORCA_CLOUD_API_KEY"


class ControlPlaneError(Exception):
    """A CLI call could not complete.

    The CLI app turns it into a message and an exit code: `exit_code` when set,
    else `output.exit_code_for_status(http_status)`. A request that got no answer
    has no status, so it sets `exit_code` instead.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        cause: Exception | None = None,
        code: str | None = None,
        extras: Mapping[str, JsonValue] | None = None,
        exit_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.http_status = http_status
        self.cause = cause
        self.code = code
        self.extras = dict(extras) if extras is not None else None
        self.exit_code = exit_code


class BackendNotResolvedError(ControlPlaneError):
    """Raised when no control plane is reachable and none was requested.

    Distinct from a usage error (bad --backend value, missing cloud creds
    after explicit --backend cloud, etc.). The catching layer in
    `orca.cli.backend` maps this to EXIT_NOT_CONNECTED so scripts can
    distinguish "no daemon and no cloud" from "you typed it wrong".
    """


@runtime_checkable
class IControlPlaneClient(Protocol):
    """What every CLI verb that works on any server calls."""

    def submit_workflow(
        self,
        workflow_name: str,
        variables: Mapping[str, OptionValue] | None = None,
        *,
        run_mode: RunModeStr,
        deployment_profile: str | None = None,
        sim_speedup: float | None = None,
    ) -> ExecutionRecordModel: ...

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
    ) -> MethodExecutionResponse: ...

    def list_executions(self) -> Sequence[ExecutionRecordModel]: ...

    def get_execution(self, execution_id: str) -> GetExecutionDetailResponse: ...

    def stop_execution(
        self, execution_id: str, *, confirm: bool = False, reason: str | None = None,
    ) -> StopExecutionResponse: ...

    def set_execution_sim_speedup(
        self, execution_id: str, sim_speedup: float,
    ) -> SetExecutionSimSpeedupResponse: ...

    def execution_remove(self, execution_id: str, *, reason: str | None = None) -> None: ...

    def device_operator_command(self, route: OperatorRoute, req: BaseModel) -> BaseModel: ...

    def device_introspection(self, device_id: str) -> DeviceIntrospectionDTO: ...

    def device_registry_list(self) -> Sequence[DeviceRegistryEntryResponse]: ...

    def device_registry_show(self, device_id: str) -> DeviceRegistryEntryResponse: ...

    def device_get_mounted_tips(self, device_id: str) -> GetMountedTipsResponse: ...

    def device_set_mounted_tips(
        self, device_id: str, mounted: list[MountedTipDTO],
        *, reason: str | None = None,
    ) -> None: ...

    def device_confirm_mounted_tips(
        self, device_id: str, *, reason: str | None = None,
    ) -> None: ...

    def device_clear_fault(
        self, device_id: str, *, reason: str | None = None,
    ) -> ClearDeviceFaultResponse: ...

    def health(self) -> bool: ...

    # -- Thread mutation (runtime-mutation-surface plan) -----------------
    # ``reason`` is required across this surface (CLI verbs enforce it via
    # ``typer.Option(...)``); the Operations layer also requires it and
    # rejects empty strings via ``_require_reason_text``.

    def thread_skip_method(
        self,
        execution_id: str,
        thread_id: str,
        *,
        method_name: str,
        reason: str,
    ) -> None: ...

    def thread_insert_method(
        self,
        execution_id: str,
        thread_id: str,
        *,
        template_name: str | None = None,
        method_code: str | None = None,
        where: InsertWhere,
        anchor: str | None = None,
        reason: str,
    ) -> None: ...

    def thread_skip_action(
        self,
        execution_id: str,
        thread_id: str,
        *,
        action_id: str | None = None,
        action_command: str | None = None,
        reason: str,
    ) -> None: ...

    def thread_insert_action(
        self,
        execution_id: str,
        thread_id: str,
        *,
        action_code: str,
        where: InsertWhere,
        anchor: str | None = None,
        reason: str,
    ) -> None: ...

    def thread_replace_method(
        self,
        execution_id: str,
        thread_id: str,
        *,
        target_name: str,
        template_name: str | None = None,
        method_code: str | None = None,
        reason: str,
    ) -> ReplaceResult: ...

    def thread_replace_action(
        self,
        execution_id: str,
        thread_id: str,
        *,
        target_command: str,
        action_code: str,
        reason: str,
    ) -> ReplaceResult: ...

    def audit_list(
        self,
        *,
        action_name: str | None = None,
        limit: int = 200,
    ) -> Sequence[AuditEntryDTO]: ...

    def events_list(
        self, *, since: int = 0, execution_id: str | None = None, limit: int = 500, tail: bool = False,
    ) -> list[PastEvent]: ...

    def intervention_next(
        self, *, after_seq: int | None, timeout_seconds: float,
    ) -> NextInterventionResponse: ...

    # -- Variables -------------------------------------------------------

    def variables_list(self, execution_id: str) -> dict[str, OptionValueJson]: ...

    def variables_get(
        self, execution_id: str, name: str, submission_id: str | None = None,
    ) -> VariableValue: ...

    def variables_set(
        self, execution_id: str, name: str, value: OptionValueJson,
        *, reason: str | None = None,
    ) -> VariableSetResponse: ...

    def variables_set_submission(
        self, execution_id: str, submission_id: str, name: str,
        value: OptionValueJson, *, reason: str | None = None,
    ) -> None: ...

    def variables_get_global(self, name: str) -> VariableValue: ...

    def variables_set_global(
        self, name: str, value: OptionValueJson, *, reason: str | None = None,
    ) -> None: ...

    def variables_unset_global(self, name: str, *, reason: str | None = None) -> bool: ...

    def variables_unset(
        self, execution_id: str, name: str, *, reason: str | None = None,
    ) -> None: ...

    def variables_unset_submission(
        self, execution_id: str, submission_id: str, name: str, *, reason: str | None = None,
    ) -> None: ...

    def variables_list_all(self) -> Sequence[CrossExecVariableDTO]: ...

    # -- Calibration registries ------------------------------------------

    def access_configs_list(self) -> Sequence[AccessConfigDTO]: ...

    def access_configs_get(self, name: str) -> AccessConfigDTO: ...

    def access_configs_add(
        self, config: AccessConfigDTO, *, reason: str | None = None,
    ) -> AccessConfigDTO: ...

    def access_configs_update(
        self, config: AccessConfigDTO, *, reason: str | None = None,
    ) -> AccessConfigDTO: ...

    def access_configs_delete(self, name: str, *, reason: str | None = None) -> None: ...

    def profiles_list(self) -> Sequence[DeploymentProfile]: ...

    def profiles_get(self, name: str) -> DeploymentProfile: ...

    def profiles_add(
        self, profile: DeploymentProfile, *, reason: str | None = None,
    ) -> DeploymentProfile: ...

    def profiles_update(
        self, profile: DeploymentProfile, *, reason: str | None = None,
    ) -> DeploymentProfile: ...

    def profiles_delete(self, name: str, *, reason: str | None = None) -> None: ...

    def grip_profiles_list(self) -> Sequence[GripProfileDTO]: ...

    def grip_profiles_get(self, labware_type: str) -> GripProfileDTO: ...

    def grip_profiles_patch(
        self, labware_type: str, body: GripProfilePatchRequest,
    ) -> GripProfileDTO: ...

    def grip_profiles_reset(self, labware_type: str, *, reason: str | None = None) -> None: ...

    def move_defaults_list(self) -> Sequence[MoveDefaultsDTO]: ...

    def move_defaults_get(self, device_id: str) -> MoveDefaultsDTO: ...

    def move_defaults_patch(
        self, device_id: str, body: MoveDefaultsPatchRequest,
    ) -> MoveDefaultsDTO: ...

    def move_defaults_reset(self, device_id: str, *, reason: str | None = None) -> None: ...

    def teachpoints_list(self, device_id: str) -> Sequence[TeachpointDTO]: ...

    def teachpoints_list_all(self) -> Sequence[TeachpointDTO]: ...

    def teachpoints_get(
        self, device_id: str, position_id: str,
    ) -> TeachpointDTO: ...

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
    ) -> TeachpointDTO: ...

    def teachpoints_set_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        body: GripProfilePatchRequest,
    ) -> TeachpointDTO: ...

    def teachpoints_clear_labware_override(
        self, device_id: str, position_id: str, labware_type: str, *, reason: str | None = None,
    ) -> None: ...

    def teachpoints_delete(
        self, device_id: str, position_id: str, *, reason: str | None = None,
    ) -> None: ...

    def teachpoints_create(
        self,
        device_id: str,
        teachpoints: Sequence[dict[str, JsonValue]],
        overwrite: bool = False,
        *, reason: str | None = None,
    ) -> CreateTeachpointsResponse: ...

    def deck_layouts_list(
        self, device_id: str | None = None,
    ) -> Sequence[DeckLayoutSummaryDTO]: ...

    def deck_layouts_get(self, device_id: str, name: str) -> DeckLayoutDTO: ...

    def deck_layouts_save(
        self, device_id: str, name: str, config: DeckLayoutConfig, *, reason: str | None = None,
    ) -> DeckLayoutDTO: ...

    def deck_layouts_delete(self, device_id: str, name: str, *, reason: str | None = None) -> None: ...

    # -- Reservations ----------------------------------------------------

    def reservations_list(self, execution_id: str | None = None) -> Sequence[ReservationSnapshotDTO]: ...

    def reservation_cancel(
        self, execution_id: str, reservation_id: str,
        reason: str,
    ) -> None: ...

    # -- Plugins ---------------------------------------------------------

    def plugins_list(self) -> Sequence[PluginDTO]: ...

    # -- Labware catalog --

    def get_labware(self, labware_type: str) -> LabwareCatalogEntry: ...

    def add_labware(
        self,
        *,
        labware_type: str,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntry: ...

    def update_labware(
        self,
        labware_type: str,
        *,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntry: ...

    def delete_labware(self, labware_type: str) -> None: ...

    def list_labware(
        self, category: str | None = None,
    ) -> Sequence[LabwareCatalogSummary]: ...

    # -- Runtime device commands -----------------------------------------

    def device_initialize(
        self, device_id: str, *,
        reason: str | None = None,
    ) -> None: ...

    def device_connect(
        self, device_id: str, *,
        reason: str | None = None,
    ) -> None: ...

    def device_disconnect(
        self, device_id: str, *,
        reason: str | None = None,
    ) -> None: ...

    def device_set_mode(
        self, device_id: str, mode: DeviceMode, *, confirm: bool = False,
    ) -> SetDeviceModeResponse: ...

    def device_setup_get(self, device_id: str) -> DeviceSetupResponse: ...

    def device_setup_put(
        self, device_id: str, setup: DeviceSetup, *, reason: str | None = None,
    ) -> DeviceSetupResponse: ...

    def device_reconcile_deck(
        self, device_id: str, *,
        reason: str | None = None,
    ) -> ReconcileDeckResponse: ...

    def device_compare_deck(self, device_id: str) -> CompareDeckResponse: ...

    def device_take_control(
        self, device_id: str, *, reason: str | None = None,
    ) -> None: ...

    def device_release_control(
        self, device_id: str, *, reason: str | None = None,
    ) -> None: ...

    # -- Threads (per-execution reads + single-thread pause/resume/recover + spawn) --

    def get_thread_detail(
        self, execution_id: str, thread_id: str,
    ) -> ThreadSnapshotResponse: ...

    def pause_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None: ...

    def resume_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None: ...

    def recover_thread(
        self, execution_id: str, thread_id: str, decision: str, *, reason: str | None = None,
    ) -> None: ...

    def spawn_thread(
        self, execution_id: str, template_name: str,
        labware_id: str | None = None, *, reason: str | None = None,
    ) -> ThreadSnapshotResponse: ...

    # -- Labware instance ops --------------------------------------------

    def labware_list(self) -> Sequence[LabwareSnapshotModel]: ...

    def labware_get_by_id(self, labware_id: str) -> LabwareSnapshotModel: ...

    def labware_get_by_id_or_none(
        self, labware_id: str,
    ) -> LabwareSnapshotModel | None: ...

    def labware_get_by_barcode(self, barcode: str) -> LabwareSnapshotModel: ...

    def labware_get_by_barcode_or_none(
        self, barcode: str,
    ) -> LabwareSnapshotModel | None: ...

    def labware_edit_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None: ...

    def labware_edit_barcode(
        self, labware_id: str, barcode: str, *, reason: str | None = None,
    ) -> None: ...

    def labware_set_carry_override(
        self, labware_id: str, patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (), *, reason: str | None = None,
    ) -> MoveParameterPatch: ...

    def labware_clear_carry_override(
        self, labware_id: str, *, reason: str | None = None,
    ) -> None: ...

    def labware_reset_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None: ...

    def labware_register(
        self, template_name: str | None = None,
        labware_type: str | None = None,
        barcode: str | None = None,
        location: str | None = None,
        reason: str | None = None,
    ) -> LabwareSnapshotModel: ...

    def check_run_labware(self, execution_id: str) -> GetCheckRunLabwareResponse:
        """A kept check run's labware, where the run left it, from its own store."""
        ...

    def labware_set_well_volumes(
        self, labware_id: str, well_volumes: dict[str, float], reason: str,
    ) -> None: ...

    def labware_resolve_contents(
        self, labware_id: str,
    ) -> ResolveContentsResponse: ...

    def labware_mark_tips_used(
        self, labware_id: str, positions: list[str], reason: str,
    ) -> MarkTipsUsedResponse: ...

    def labware_set_tip_state(
        self, labware_id: str, tip_positions_present: list[str], reason: str,
    ) -> None: ...

    def labware_confirm_tip_state(
        self, labware_id: str, reason: str | None = None,
    ) -> None: ...

    def release_mover_hold(
        self, device_id: str, reason: str,
        to_location: str | None = None, force: bool = False,
    ) -> ReleaseMoverHoldResponse: ...

    def labware_confirm_well_volumes(
        self, labware_id: str, reason: str | None = None,
    ) -> None: ...

    # -- Submissions -----------------------------------------------------

    def submission_submit(
        self, request: SubmitExecutionRequest,
    ) -> SubmitExecutionResponse: ...

    def submissions_list(
        self, execution_id: str | None = None,
    ) -> Sequence[SubmissionDTO]: ...

    def submission_get(self, submission_id: str) -> SubmissionDTO: ...

    # -- Catalog reads + system info -------------------------------------

    def list_thread_templates(self) -> Sequence[ThreadTemplateDTO]: ...

    def system_info(self) -> GetSystemInfoResponse: ...

    def methods_list(self) -> Sequence[MethodTemplateDTO]: ...

    def method_get(
        self, name: str, workflow_name: str | None = None,
    ) -> MethodTemplateDTO: ...

    def workflow_get(self, name: str) -> WorkflowTemplateDTO: ...

    def list_locations(self) -> Sequence[LocationDTO]: ...

    def topology_get(
        self, source: bool = False,
    ) -> TopologyViewResponse | TopologySourceResponse: ...

    def runtime_status(self) -> RuntimeStatusResponse: ...

    def runtime_reload(self, reason: str = "") -> ReloadResponse:
        """Rebuild the runtime from the package files on disk."""
        ...

    # -- Recoverable-timeout operator decisions ---------------------------

    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float, *,
        reason: str | None = None,
    ) -> RecoverableTimeoutDecisionResponse: ...

    def recoverable_timeout_abort(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse: ...

    def recoverable_timeout_mark_complete(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse: ...

    # -- Ops-history reads -----------------------------------------------

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
    ) -> SearchOpsHistoryResponse: ...

    # -- Labware journey + runtime mutations -----------------------------

    def labware_journey(
        self, labware_id: str, kinds: Sequence[str] | None = None,
    ) -> GetLabwareJourneyResponse: ...

    def labware_load_run(self, request: LoadRunLabwareRequest) -> LoadRunLabwareResponse: ...

    def labware_clear_submission(
        self, submission_id: str, force: bool = False, *, reason: str | None = None,
    ) -> dict[str, list[str]]: ...

    def labware_discharge(
        self, labware_id: str, force: bool = False, *, reason: str | None = None,
    ) -> dict[str, list[str]]: ...

    def labware_clear_all(
        self, force: bool = False, *, reason: str | None = None,
    ) -> dict[str, list[str]]: ...

    def state_unsettled(self) -> UnsettledStateResponse: ...

    def execution_close(
        self, execution_id: str, *, reason: str | None = None,
    ) -> CloseExecutionResponse: ...

    def pause_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> None: ...

    def resume_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> ResumeExecutionResult: ...

    def manual_steps_list(
        self, execution_id: str | None,
    ) -> list[PendingManualStepDTO]: ...

    def manual_step_confirm(
        self, execution_id: str, step_id: str, *, reason: str | None = None,
    ) -> None: ...

    def incidents_list(
        self,
        unacknowledged_only: bool = False,
        category: str | None = None,
        execution_id: str | None = None,
    ) -> list[IncidentResponse]: ...

    def incidents_get(self, incident_id: str) -> IncidentResponse: ...

    def incidents_ack(
        self, incident_id: str, *, reason: str | None = None,
    ) -> IncidentAckResponse: ...

    def incidents_ack_all(
        self, category: str | None = None, *, reason: str | None = None,
    ) -> IncidentAckResponse: ...

    def list_workflows(self) -> Sequence[WorkflowTemplateDTO]: ...


class SubmittedModule(Protocol):
    """What the hosted-only verbs read from the answer to a submission."""

    @property
    def name(self) -> str: ...

    @property
    def commit_sha(self) -> str | None: ...

    def model_dump(self, *, mode: str = "python") -> dict[str, JsonValue]: ...


@runtime_checkable
class ICloudControlPlaneClient(IControlPlaneClient, Protocol):
    """The verbs only a hosted server has: loading a workflow file and submitting the topology."""

    def __init__(self, *, base_url: str, api_key: str) -> None: ...

    @property
    def base_url(self) -> str: ...

    def workflow_load(
        self, name: str, source: str, message: str, *, reason: str | None = None,
    ) -> SubmittedModule: ...

    def topology_submit(
        self, source: str, message: str,
    ) -> SubmittedModule: ...
