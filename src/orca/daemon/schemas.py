"""Pydantic request/response DTOs for the daemon's HTTP boundary.

Every JSON response returned from a FastAPI route is one of these models.
Every JSON request body accepted by a route is one of these models. This
satisfies the "JSON = Pydantic" rule and gives clients a stable
schema independent of internal dataclass shapes.

Enum fields stay as Enum instances on the model attribute (so callers do
`body.status == ExecutionState.RUNNING`, not `body.status == "running"`),
and Pydantic v2 serializes them to their `.value` on JSON dump.

Outgoing conversion for dataclass-based DTOs: `from_dc(dc)` runs
`dataclasses.asdict` then `model_validate`. `extra="forbid"` makes schema
drift a loud failure: a stray key raises instead of being silently dropped.

In-process callers (a hosted deployment, tests) never need these DTOs; they consume the
dataclasses directly from `ISystemRuntime`.
"""

import dataclasses
from enum import Enum
from typing import Any, Literal, Union

from typing_extensions import Self, TypeGuard

from cheshire_drivers.gateway_protocol import EffectiveMode
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from cheshire_drivers.teachpoints import (
    AccessConfig as AccessConfigDTO,
    Teachpoint,
)
from orca.runtime.move_parameter_models import (
    GripProfilePatchRequest,
    MoveDefaultsPatchRequest,
    LabwareGripProfile as GripProfileDTO,
    TransporterMoveDefaults as MoveDefaultsDTO,
)
from orca.runtime.teachpoint_wire import coord_type_for, typed_to_wire
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    field_serializer,
    model_validator,
)

from orca.runtime.execution_record import ExecutionState
from orca.runtime.incident_store import SystemIncident
from orca.state.placement import PlacementState
from orca.runtime.labware_catalog_store import LabwareCatalogSummary
from orca.runtime.run_modes import WorkflowRunMode
from orca.state.provenance import Provenance
from orca.runtime.status_models import ConnectionCard, TopologyCard
from orca.runtime.runtime_state import RuntimeState
from orca.variables.resolution import VariableResolution, VariableSource
from orca.workflow_models.status_enums import FailurePolicy


# -- Shared enums ------------------------------------------------------------


class OperationResult(Enum):
    """Wire-level enum for action-route success responses.

    Each "did-something" route returns `{status: <OperationResult.value>}`.
    Using an enum means a typo like `OperationResult.ABORETD` fails at
    import time, and tests assert `body.status == OperationResult.ABORTED`
    with attribute access instead of magic strings.
    """
    STOPPED = "stopped"
    UNLOADED = "unloaded"
    ABORTED = "aborted"
    PAUSING = "pausing"
    RESUMED = "resumed"
    RECOVERED = "recovered"
    MUTATED = "mutated"


# -- Base DTO ----------------------------------------------------------------


class _BaseDTO(BaseModel):
    """Base config for DTOs that mirror frozen dataclasses.

    Enum fields: we do NOT set `use_enum_values=True`. Pydantic v2 keeps the
    Enum instance on the model attribute (so tests can assert
    `body.status == ExecutionState.RUNNING`), and `model_dump(mode="json")`
    serializes the Enum to its `.value` on the wire -- the best of both.

    `extra="forbid"` rejects unknown keys. This turns schema drift into a
    loud failure: if a runtime dataclass gains a field that no DTO mirrors,
    `from_dc` will error on the stray key instead of silently dropping it.
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_dc(cls, obj: object) -> Self:
        if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
            raise TypeError(f"expected a dataclass instance, got {type(obj).__name__}")
        return cls.model_validate(dataclasses.asdict(obj))


# -- Request bodies ----------------------------------------------------------


OptionValueJson = Union[str, int, float, bool]

# Wire-level shape of the submit-time run-mode override. Kept as a Literal
# alias (rather than importing WorkflowRunMode) so request DTOs stay
# JSON-serializable without an enum dependency on the wire. Daemon-side
# conversion to WorkflowRunMode happens at the route handler.
RunModeStr = Literal["PURE_SIM", "DEVICE_SIM", "LIVE"]

# Tuple form for runtime membership checks; the source of truth for
# valid wire-level run-mode strings, used by CLI input narrowing.
_VALID_RUN_MODES: tuple[RunModeStr, ...] = ("PURE_SIM", "DEVICE_SIM", "LIVE")


def is_run_mode_str(value: str) -> TypeGuard[RunModeStr]:
    """``TypeGuard`` narrowing an arbitrary ``str`` to ``RunModeStr``.

    Used by CLI verbs that accept ``--run-mode`` as an unbounded string
    and need to narrow to the Literal before passing through to clients
    whose signatures are typed ``RunModeStr | None``. The runtime check
    plus the TypeGuard is the project-blessed alternative to ``cast``.
    """
    return value in _VALID_RUN_MODES


class SubmitWorkflowRequest(BaseModel):
    """POST /executions body.

    `profile_path`: if set, the daemon reads the JSON deployment profile
    at this path (on the daemon host's filesystem) and loads it into the
    new execution's variable partition immediately after submission, before
    any thread runs its first tick. Because the daemon binds to 127.0.0.1
    only, path-based loading is safe: the CLI and daemon share a filesystem.

    `run_mode`: REQUIRED per-submission selector.
    PURE_SIM / DEVICE_SIM / LIVE. Combined per-device with topology
    `sim_override` via the 12-row resolver. No deployment-level fallback.

    `acknowledge_warnings`: bypasses the
    `LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED` gate. Default False
    causes the runtime to refuse the submission and return the override
    list so the operator can review.
    """
    workflow_name: str
    variables: dict[str, OptionValueJson] | None = None
    profile_path: str | None = None
    run_mode: RunModeStr
    acknowledge_warnings: bool = False


class SubmitMethodExecutionRequest(BaseModel):
    """POST /method-executions body for standalone method execution."""
    workflow_name: str
    method_name: str
    labware_start: dict[str, str]
    labware_end: dict[str, str]
    variables: dict[str, OptionValueJson] | None = None
    run_mode: RunModeStr
    acknowledge_warnings: bool = False


# The legacy thread-mutation REST surface and its Pydantic request DTOs are
# gone. The unified Operations surface owns the wire
# shape now -- see ``orca.operations.thread`` for ``PauseRequest`` /
# ``ResumeRequest`` / ``SpawnThreadRequest`` / ``RecoverThreadRequest``
# and the SkipMethod / AbortMethod / InsertMethod / SkipAction /
# InsertAction request models, including the typed body validation
# the legacy ``_MutationRequestBase`` empty-string-to-None coercer
# previously provided.


class LabwareEditLocationRequest(BaseModel):
    """POST /labware/by-id/{id}/edit-location body."""
    location: str
    reason: str


class LabwareEditBarcodeRequest(BaseModel):
    """PUT /labware/by-id/{id}/barcode body."""
    barcode: str


class LabwareResetLocationRequest(BaseModel):
    """POST /labware/by-id/{id}/reset-location body."""
    location: str
    reason: str


class LabwareCatalogCreateRequest(BaseModel):
    """POST /labware/catalog body. ``source`` is forced operator_custom."""
    labware_type: str
    display_name: str
    category: str
    vendor: str | None = None
    plr_class_name: str | None = None
    geometry: dict[str, JsonValue]


class LabwareCatalogUpdateRequest(BaseModel):
    """PUT /labware/catalog/{labware_type} body (labware_type from the path)."""
    display_name: str
    category: str
    vendor: str | None = None
    plr_class_name: str | None = None
    geometry: dict[str, JsonValue]


class LabwareCatalogListResponse(BaseModel):
    """GET /labware response: flat list of catalog rows, geometry-free.

    Rows are ``LabwareCatalogSummary`` (no geometry); fetch the geometry blob
    per row via ``GET /labware/{labware_type}``.
    """
    labware: list[LabwareCatalogSummary]


class DeviceExecuteRequest(BaseModel):
    """POST /devices/{name}/execute body."""
    command: str
    options: dict[str, JsonValue] | None = None
    mode: WorkflowRunMode | None = None
    confirm: bool = False
    """Acknowledgement for a command the driver flagged `requires_confirm`:
    the raw vendor console and its aliases. Everything else dispatches
    without it. Separate from the audit confirm this route always supplies."""


class DeviceInvokeRequest(BaseModel):
    """POST /devices/{name}/invoke body."""
    capability: str
    kwargs: dict[str, JsonValue] | None = None
    mode: WorkflowRunMode | None = None
    confirm: bool = False
    """Acknowledgement for a command the driver flagged `requires_confirm`:
    the raw vendor console and its aliases. Everything else dispatches
    without it. Separate from the audit confirm this route always supplies."""


class SubmissionSubmitRequest(BaseModel):
    """POST /submissions body. Serialization of LabwareGroup(s) + workflow name.

    Each group carries its id, optional name, and one entry per thread template.
    Acquisition variants: `pool` (default), `barcode`, `location`.

    `run_mode`: REQUIRED per-submission selector.
    PURE_SIM / DEVICE_SIM / LIVE. Combined per-device with topology
    `sim_override` via the 12-row resolver. No deployment-level fallback.

    `acknowledge_warnings`: bypasses the
    `LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED` gate.
    """
    workflow_name: str
    groups: list["LabwareGroupDTO"] = []
    variables: dict[str, OptionValueJson] | None = None
    batch_mode: Literal["STANDALONE", "JOIN_EXISTING"] = "STANDALONE"
    operator_id: str | None = None
    deployment_profile: str | None = None
    run_mode: RunModeStr
    acknowledge_warnings: bool = False


class LabwareGroupDTO(BaseModel):
    """Wire form of LabwareGroup."""
    id: str
    members: list["LabwareGroupMemberDTO"]
    name: str | None = None


class LabwareGroupMemberDTO(BaseModel):
    """Wire form of LabwareGroupMember."""
    thread_template_name: str
    acquisition: "AcquisitionDTO"


class AcquisitionDTO(BaseModel):
    """Discriminated-union form for Acquisition.

    `kind`: "pool" -> no further fields; "barcode" -> barcode; "location"
    -> source_location + optional verify_barcode.
    """
    kind: Literal["pool", "barcode", "location"] = "pool"
    barcode: str | None = None
    source_location: str | None = None
    verify_barcode: str | None = None


class MountTopologyRequest(BaseModel):
    """POST /mount-topology body. `spec` is `module:factory` naming a
    `build_topology(stores)` builder that returns a `Topology`. `sim=True`
    runs the runtime in simulation."""
    spec: str
    sim: bool = False


class RegisterWorkflowRequest(BaseModel):
    """POST /workflows body. `spec` is `module:factory` naming a
    `build_workflow(topology)` builder that returns a `WorkflowTemplate`.
    The workflow is registered against the mounted topology; no execution
    starts."""
    spec: str


# -- Query-param DTOs --------------------------------------------------------
#
# Client-side request shapes for endpoints whose inputs ride as querystring
# rather than a JSON body. Declaring them here keeps the client off of
# key-by-key dict construction and gives httpx a single `.model_dump(
# exclude_none=True)` call to produce the params mapping.


class IncidentListQuery(BaseModel):
    """Query params for GET /incidents."""
    unacknowledged_only: bool = False
    category: str | None = None
    execution_id: str | None = None


class IncidentAckAllQuery(BaseModel):
    """Query params for POST /incidents/ack-all."""
    category: str | None = None


class ReservationCancelQuery(BaseModel):
    """Query params for DELETE /executions/{id}/reservations/{rsv_id}."""
    reason: str | None = None


# -- Lifecycle response DTOs -------------------------------------------------


class HealthResponse(BaseModel):
    """GET /health body. Always 200 while the daemon is up; field values tell
    callers whether a system is loaded and what state the runtime is in.

    `runtime_state` rides as its canonical name string on the wire because
    `RuntimeState` is `(str, Enum)` with `.value == .name`; Pydantic v2's
    native string-to-enum-value coercion handles inbound parsing.

    `mounting` names the spec of a mount still in progress, so a caller whose
    mount request timed out can see it is still running before retrying.
    """
    daemon: Literal["ok"] = "ok"
    system_loaded: bool
    runtime_state: RuntimeState | None
    spec: str | None
    sim: bool
    mounting: str | None = None


class MountTopologyResponse(BaseModel):
    """POST /mount-topology success body. `runtime_state` is the post-mount
    RuntimeState.

    `runtime_state` rides as its canonical name string on the wire because
    `RuntimeState` is `(str, Enum)` with `.value == .name`; Pydantic v2's
    native string-to-enum-value coercion handles inbound parsing.
    """
    spec: str
    sim: bool
    runtime_state: RuntimeState


class RegisterWorkflowResponse(BaseModel):
    """POST /workflows success body. Carries the registered workflow's name."""
    workflow_name: str


# -- Action-result response DTOs --------------------------------------------
#
# Each "did-something" route returns {status: <OperationResult>}. Typing as
# the enum means:
#   - Route code: `return ShutdownResponse()` picks up the class's default
#     OperationResult member; typos fail at import time.
#   - Tests: `body.status == OperationResult.STOPPED` is attribute access,
#     not string lookup, and catches wire-format drift.


class _ActionResponse(_BaseDTO):
    """Action responses share one field. Concrete subclasses pin a default."""
    status: OperationResult


class ShutdownResponse(_ActionResponse):
    """POST /shutdown success body."""
    status: OperationResult = OperationResult.STOPPED


class UnloadResponse(_ActionResponse):
    """POST /unload success body."""
    status: OperationResult = OperationResult.UNLOADED


class ExecutionStopResponse(_ActionResponse):
    """DELETE /executions/{id} success body."""
    status: OperationResult = OperationResult.ABORTED


class ThreadsPauseResponse(_ActionResponse):
    """POST /executions/{id}/pause or /threads/{tid}/pause success body."""
    status: OperationResult = OperationResult.PAUSING


class ThreadResumeResponse(_ActionResponse):
    """POST /executions/{id}/threads/{tid}/resume success body."""
    status: OperationResult = OperationResult.RESUMED


class ThreadMutationResponse(_ActionResponse):
    """Shared response for skip/abort/insert mutation routes."""
    status: OperationResult = OperationResult.MUTATED


class AuditEntryDTO(BaseModel):
    """One @dangerous-confirmed operation as recorded in the in-memory
    AuditTrail (the durable file mirror lives at log_dir/orca_audit.log).
    """
    timestamp: float
    action_name: str
    danger_level: str
    reason: str | None
    call_args: dict[str, JsonValue]


class AuditListResponse(BaseModel):
    """GET /audit response. `entries` is most-recent-last (matches the
    in-memory ring buffer's append order)."""
    entries: list[AuditEntryDTO]


class ThreadRecoverResponse(_ActionResponse):
    """POST /executions/{id}/threads/{tid}/recover success body."""
    status: OperationResult = OperationResult.RECOVERED


class ReservationCancelResponse(BaseModel):
    """DELETE /executions/{id}/reservations/{rsv_id} response."""
    status: Literal["cancelled"] = "cancelled"


class ExecutionRemoveResponse(BaseModel):
    """POST /executions/{id}/remove response."""
    status: Literal["removed"] = "removed"


# -- Error response DTOs -----------------------------------------------------


class ErrorResponse(BaseModel):
    """FastAPI's HTTPException serialization shape -- `{detail: str}`.

    Not a route response_model (FastAPI emits this automatically); defined
    here so clients and tests can parse error bodies with type safety
    instead of dict[str,str] key lookups.
    """
    detail: str


class ValidationErrorItem(BaseModel):
    """One entry in FastAPI/Pydantic's 422 response body."""
    type: str
    loc: list[str | int]
    msg: str


class ValidationErrorResponse(BaseModel):
    """Top level of a 422 body."""
    detail: list[ValidationErrorItem]


# -- Dataclass-mirror response DTOs -----------------------------------------


class ActionSnapshotDTO(_BaseDTO):
    id: str
    command: str
    status: str
    position_id: str
    resource_name: str
    description: str | None = None


class MethodSnapshotDTO(_BaseDTO):
    id: str
    name: str
    status: str
    current_action: ActionSnapshotDTO | None
    completed_action_count: int


class ThreadSnapshotDTO(_BaseDTO):
    id: str
    name: str
    status: str
    current_location: str
    current_method: MethodSnapshotDTO | None
    completed_method_count: int
    last_error: str | None
    pause_reason: str | None
    completed_methods: tuple[str, ...]
    labware_template_name: str | None = None
    labware_id: str | None = None
    labware_name: str | None = None
    paused_device_command: str | None = None
    pause_message: str | None = None
    pause_site: str | None = None
    """Mirror of ``ThreadSnapshot.pause_site``: WHERE the thread stopped."""
    honoured_decisions: tuple[str, ...] = ()
    """The recovery decisions this thread will accept right now, and the only
    ones to offer. Empty when it is not error-paused. Anything else is refused
    and the thread stays paused."""
    waiting_for: str | None = None


class ReservationSnapshotDTO(_BaseDTO):
    position_id: str
    reservation_id: str
    # None for reservations not owned by any thread (system-held / manual).
    # Wire-shape change from prior str typing that masked None as
    # the literal "unknown".
    thread_id: str | None
    # Populated by the cross-execution ``GET /reservations`` route. The
    # per-execution route leaves it null: the URL already pins the scope.
    execution_id: str | None = None
    # What the hold is for, and what kind of hold it is: a person being asked
    # to place that labware, or that labware on its way here. Neither, and it
    # is a hold that rests nothing at this position.
    labware_name: str | None = None
    awaiting_operator: bool = False
    arriving: bool = False


class CrossExecVariableDTO(_BaseDTO):
    """One row of the cross-execution variable listing."""
    execution_id: str
    name: str
    value: str | int | float | bool


class ExecutionDetailDTO(_BaseDTO):
    id: str
    workflow_name: str
    status: str
    error: str | None
    threads: list[ThreadSnapshotDTO]
    total_thread_count: int
    completed_thread_count: int
    active_thread_count: int
    paused: bool = False
    """Whether the execution-level pause latch is set. Separate from
    ``status``, which a pause never changes."""
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort."""


class ExecutionRecordDTO(_BaseDTO):
    """Maps `orca.runtime.execution_record.ExecutionRecord` (dataclass + Enum).

    ``status`` is a plain string on the wire. The legacy daemon route
    surfaced only the four ``ExecutionState`` values (running / completed
    / failed / aborted); reads moved to the Operations surface, which
    carries the richer ``ExecutionPhase`` vocabulary
    (accepting / draining / completed / failed / aborted). Bug FFF:
    summary and detail must agree, so both surfaces emit the rich phase
    when the runtime is the source of truth and the flatter state when
    rehydrated from a terminal DB row. Accept both as plain ``str``.
    """
    id: str
    workflow_name: str
    status: str
    error: str | None = None
    paused: bool = False
    """Whether the execution-level pause latch is set. Separate from
    ``status``, which a pause never changes."""
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort."""


class SystemInfoDTO(_BaseDTO):
    """Mirror of SystemInfoSnapshot.

    Sim-hierarchy v3.4: per-submission run_mode supersedes a system-wide
    is_simulating flag. Operators read effective_mode on per-device or
    per-submission DTOs instead.
    """
    name: str
    description: str
    version: str


class WorkflowTemplateDTO(_BaseDTO):
    """Mirror of WorkflowTemplateSnapshot."""
    name: str
    entry_thread_template_names: tuple[str, ...]


class MethodTemplateDTO(_BaseDTO):
    """Mirror of MethodTemplateSnapshot.

    `failure_policy` stays as the `FailurePolicy` enum on the model attribute
    so callers assert `dto.failure_policy == FailurePolicy.PAUSE` with
    attribute access. Since `FailurePolicy` is `(str, Enum)` with
    `.value == .name`, the wire form is the canonical name string via
    Pydantic v2's default enum serialization; inbound name strings are
    accepted via the standard string-to-enum-value coercion.
    """
    workflow_name: str
    name: str
    failure_policy: FailurePolicy


class ThreadTemplateDTO(_BaseDTO):
    """Mirror of ThreadTemplateSnapshot."""
    workflow_name: str
    name: str
    labware_template_name: str
    start_position_id: str
    end_position_ids: list[str]


class LocationDTO(_BaseDTO):
    """Mirror of LocationSnapshot."""
    name: str
    resource_name: str | None
    loaded_labware_ids: tuple[str, ...]
    deck_sites: tuple[str, ...] = ()


def _to_json_value(value: Any) -> JsonValue:
    """Normalize a ``dataclasses.asdict`` payload to JsonValue.

    Detail dataclasses carry ``tuple[str, ...]`` fields (e.g.
    ``CoLabwareTimeoutDetail.missing_labware_names``,
    ``DeadlockDetail.cycling_thread_ids``). ``asdict`` preserves those as
    tuples, but ``JsonValue`` forbids tuples (JSON has no tuple type; a
    tuple serializes as a JSON array). Convert tuples to lists so the
    ``detail`` dict validates -- the wire shape is identical either way.
    """
    if isinstance(value, dict):
        return {str(k): _to_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(v) for v in value]
    return value


class IncidentDTO(BaseModel):
    """Mirror of SystemIncident. `detail` is kept as a dict (the typed
    detail union expands to varied dataclass shapes; callers cast as needed).

    `category`, `severity`, `recovery_action` ride as string names.
    """
    id: str
    timestamp: float
    category: str
    severity: str
    execution_id: str | None
    thread_id: str | None
    message: str
    detail: dict[str, JsonValue]
    recovery_action: str
    acknowledged: bool

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_incident(cls, inc: SystemIncident) -> Self:
        """Convert a SystemIncident (frozen dataclass) to DTO; enum fields
        come out as their names (.name, not .value)."""
        detail = _to_json_value(dataclasses.asdict(inc.detail))
        assert isinstance(detail, dict)
        return cls(
            id=inc.id,
            timestamp=inc.timestamp,
            category=inc.category.name,
            severity=inc.severity.name,
            execution_id=inc.execution_id,
            thread_id=inc.thread_id,
            message=inc.message,
            detail=detail,
            recovery_action=inc.recovery_action.name,
            acknowledged=inc.acknowledged,
        )


class IncidentAckResponse(BaseModel):
    """POST /incidents/{id}/ack and /incidents/ack-all response."""
    acknowledged_count: int


class RecoverableTimeoutExtendRequest(BaseModel):
    """POST /incidents/{id}/recoverable_timeout/extend body."""
    additional_seconds: float = Field(gt=0)


class RecoverableTimeoutOperatorRequest(BaseModel):
    """POST /incidents/{id}/recoverable_timeout/{abort,mark_complete} body."""
    operator_name: str
    reason: str


class RecoverableTimeoutDecisionResponse(BaseModel):
    """Response for the three recoverable-timeout decision endpoints."""
    incident_id: str
    decision: str


class VariableValue(BaseModel):
    """GET /variables/{execution_id}/{name} body. Single-var value wrapper.

    ``execution_id`` is None for global-scope reads
    (``GET /variables/global/{name}``) which have no execution context.

    ``value`` is what a thread whose submission holds no override resolves.
    ``shadowed_by`` names the submissions that resolve something else, so a
    reader is never handed a value the run will not use without being told.
    """
    name: str
    execution_id: str | None = None
    value: OptionValueJson
    source: VariableSource
    shadowed_by: list[str] = Field(default_factory=list)


class SubmissionOverrideDTO(BaseModel):
    """One submission's own value for a variable."""
    submission_id: str
    value: OptionValueJson


class VariableResolutionResponse(BaseModel):
    """GET /variables/{execution_id}/{name}/resolution body.

    Answers "what will this run actually use" for every submission at once:
    ``value`` / ``source`` for submissions with no override, and one entry per
    submission that outranks them. A batched or multi-group execution can hold
    a different value per submission, so a single answer would be wrong for
    all but one of them.
    """
    name: str
    execution_id: str
    value: OptionValueJson | None = None
    source: VariableSource | None = None
    overrides: list[SubmissionOverrideDTO] = Field(default_factory=list)

    @classmethod
    def from_resolution(cls, resolution: VariableResolution) -> "VariableResolutionResponse":
        return cls(
            name=resolution.name,
            execution_id=resolution.execution_id,
            value=resolution.value,
            source=resolution.source,
            overrides=[
                SubmissionOverrideDTO(submission_id=o.submission_id, value=o.value)
                for o in resolution.overrides
            ],
        )


class VariablesListResponse(RootModel[dict[str, OptionValueJson]]):
    """GET /variables/{execution_id} response: name -> value map."""


class VariableSetRequest(BaseModel):
    """PUT /variables/{execution_id}/{name} body."""
    value: OptionValueJson


class VariableUnsetResponse(BaseModel):
    """DELETE /variables/{execution_id}/{name} response.

    ``existed`` disambiguates "I just unset a value that was set"
    (``True``) from "no-op; the name was already absent" (``False``).
    DELETE remains idempotent on the wire (HTTP 200 either way) so
    callers don't need to handle 404 retry logic, but the bool gives
    the operator the signal that GET would also have given them
    without doing a separate round-trip (Bug QQQ). Defaults to None
    for back-compat with daemon clients pre-dating the field; new
    hosted REST writes always populate it.
    """
    status: Literal["unset"] = "unset"
    existed: bool | None = None


class VariableSetResponse(BaseModel):
    """PUT /variables/{execution_id}/{name} response.

    ``shadowed_by`` names the submissions whose own value still outranks the
    write, so a 200 never reads as "the run will now use this" when it will
    not. Empty for a submission-scope or global write.
    """
    status: Literal["set"] = "set"
    name: str
    value: OptionValueJson
    shadowed_by: list[str] = Field(default_factory=list)


class PluginDTO(_BaseDTO):
    """Mirror of PluginSnapshot."""
    type_name: str
    disabled: bool
    exposed_command_names: tuple[str, ...]


class PluginCommandDTO(BaseModel):
    """Wire-format for a PluginCommand, sans the callable `handler`.

    `handler` is dropped -- it's a local Python callable, not serializable.
    Clients invoke the command via POST /plugins/commands/{name} (not in MVP).
    """
    name: str
    description: str
    usage: str

    model_config = ConfigDict(extra="forbid")


class LabwareDTO(_BaseDTO):
    """Mirror of LabwareSnapshot."""
    id: str
    name: str
    template_name: str
    barcode: str | None
    current_location: str | None
    placement: PlacementState | None = None
    """EXPECTED means `current_location` is where it is HEADED, not where it is."""
    carry_override: MoveParameterPatch = Field(default_factory=MoveParameterPatch)
    contents_provenance: Provenance = Provenance.UNKNOWN

    @field_serializer("carry_override")
    def _only_what_was_said(self, patch: MoveParameterPatch) -> dict[str, JsonValue]:
        """Sparse: a null would read as an opinion nobody stated."""
        return patch.model_dump(exclude_none=True)


class LocationEventDTO(_BaseDTO):
    """Mirror of LocationEvent (one step of a labware's location history)."""
    sequence: int
    position_id: str
    timestamp: float


class TeachpointDTO(BaseModel):
    """Mirror of cheshire_drivers.teachpoints.Teachpoint, scoped per-device.

    `position_id` is the world-relative deck-slot identifier this teachpoint
    targets. `coord_type` is the cartesian/joint discriminator; `coords` is
    the flat wire dict of axis values only (no `type`/`orientation` keys, which
    ride at the top level) so CLI clients render coordinates without
    reconstructing typed coordinate models.
    """
    model_config = ConfigDict(extra="forbid")

    device_id: str
    position_id: str
    coord_type: str
    coords: dict[str, float | str]
    access_config_name: str | None
    gateway: str | None
    orientation: str | None
    taught_with: str | None = None
    by_labware: dict[str, MoveParameterPatch] = Field(default_factory=dict)

    @classmethod
    def from_value(cls, device_id: str, value: Teachpoint) -> Self:
        return cls(
            device_id=device_id,
            position_id=value.position_id,
            coord_type=coord_type_for(value),
            coords=typed_to_wire(value),
            access_config_name=value.access_config_name,
            gateway=value.gateway,
            orientation=value.orientation,
            taught_with=value.taught_with,
            by_labware=dict(value.by_labware),
        )

    @field_serializer("by_labware")
    def _only_what_each_labware_claims(
        self, by_labware: dict[str, MoveParameterPatch],
    ) -> dict[str, dict[str, JsonValue]]:
        """Sparse on the wire: a null would read as an opinion nobody holds."""
        return {
            labware_type: patch.model_dump(exclude_none=True)
            for labware_type, patch in by_labware.items()
        }


class CreateTeachpointRequest(BaseModel):
    """POST /teachpoints body. Flat wire envelope: `coord_type` and
    `orientation` ride at the top level, never inside `coords`."""
    model_config = ConfigDict(extra="forbid")

    device_id: str
    position_id: str
    coord_type: Literal["cartesian", "joint"]
    coords: dict[str, str | int | float]
    access_config_name: str | None = None
    gateway: str | None = None
    orientation: Literal["left", "right"] | None = None
    taught_with: str | None = None
    by_labware: dict[str, MoveParameterPatch] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _orientation_matches_coord_type(self) -> Self:
        if self.coord_type == "cartesian" and self.orientation is None:
            raise ValueError(
                "Cartesian teachpoints require orientation ('left' or 'right')"
            )
        return self


class UpdateTeachpointRequest(BaseModel):
    """PUT /teachpoints/{device_id}/{position_id} body. `coords` is replaced;
    access_config/gateway/orientation update only when the matching
    update_* flag is set. coord_type is preserved from the existing row."""
    model_config = ConfigDict(extra="forbid")

    coords: dict[str, str | int | float]
    access_config_name: str | None = None
    gateway: str | None = None
    orientation: Literal["left", "right"] | None = None
    taught_with: str | None = None
    update_access_config: bool = False
    update_gateway: bool = False
    update_orientation: bool = False
    update_taught_with: bool = False

    @model_validator(mode="after")
    def _orientation_set_requires_value(self) -> Self:
        if self.update_orientation and self.orientation is None:
            raise ValueError(
                "update_orientation=true requires orientation ('left' or 'right')"
            )
        return self


class DeckLayoutSummaryDTO(BaseModel):
    """Lightweight summary for `orca deck-layouts list`."""
    model_config = ConfigDict(extra="forbid")

    device_id: str
    name: str
    deck_type: str


class DeckLayoutDTO(BaseModel):
    """Full deck layout for `orca deck-layouts show`."""
    model_config = ConfigDict(extra="forbid")

    device_id: str
    name: str
    config: DeckLayoutConfig


class DeviceFaultDTO(_BaseDTO):
    """Mirror of ``DeviceFaultSummary``.

    ``outcome`` is ``failed`` when the driver reported the failure itself and
    ``unknown`` when no answer came back at all. On ``unknown`` the command may
    still be running on the instrument.
    """
    command: str
    outcome: Literal["failed", "unknown"]
    error: str
    error_type: str
    at: float
    may_still_be_moving: bool
    message: str
    execution_id: str | None = None


class DeviceDTO(_BaseDTO):
    """Mirror of DeviceSnapshot.

    Sim-hierarchy v3.4: `effective_mode` replaces the legacy
    `is_simulating: bool`. Operators read PURE_SIM / DEVICE_SIM / LIVE
    directly; sim/wire/hardware fanout is downstream.

    `effective_mode` is the v3.4 12-row resolved per-device mode, combining
    the device's `sim_override` with the base the reader means. A device row
    means the operator's world, so it does not track a run in force. See
    `DeviceSnapshot` for the full contract.
    """
    name: str
    type_name: str
    is_initialized: bool
    is_busy: bool
    effective_mode: WorkflowRunMode
    position_ids: tuple[str, ...]
    loaded_labware_ids: tuple[str, ...]
    # Mirror of ``DeviceSnapshot.under_external_control``: True when the
    # hosted device-integration gateway holds the device for ad-hoc
    # troubleshooting. Surfaces on the wire so MCP / REST clients can
    # tell operators "device 'X' is currently under gateway control"
    # without a separate fetch.
    under_external_control: bool = False
    external_control_hold: str | None = None
    """Mirror of ``DeviceSnapshot.external_control_hold``: why an operator is
    holding this, or None if nobody is."""
    fault: DeviceFaultDTO | None = None
    """Mirror of ``DeviceSnapshot.fault``: the command that left this device
    part-way through something, or None. The workflow cannot drive the device
    while it is set; an operator clears it with ``clear-device-fault``."""


class TransporterDTO(_BaseDTO):
    """Mirror of TransporterSnapshot.

    Returned by the registry list endpoint. ``current_labware_id`` is the
    labware in the gripper right now (None when the transporter is empty).
    """
    name: str
    type_name: str
    is_busy: bool
    position_ids: tuple[str, ...]
    current_labware_id: str | None
    # Mirror of ``TransporterSnapshot.under_external_control``. True when
    # the hosted gateway holds the transporter for teach-point retake or
    # other ad-hoc operator work. ``extra="forbid"`` would reject the new
    # field otherwise.
    under_external_control: bool = False
    external_control_hold: str | None = None
    """Mirror of ``DeviceSnapshot.external_control_hold``: why an operator is
    holding this, or None if nobody is."""


class MoverDTO(_BaseDTO):
    """Mirror of MoverSnapshot.

    Every plate-mover, the liquid handler's own gripper included, which the
    transporter list leaves out. ``current_labware_id`` is what that gripper is
    holding right now.
    """
    name: str
    type_name: str
    is_busy: bool
    gripper_position_id: str
    current_labware_id: str | None
    under_external_control: bool = False
    external_control_hold: str | None = None
    """Mirror of ``DeviceSnapshot.external_control_hold``: why an operator is
    holding this, or None if nobody is."""


class ResourcePoolDTO(_BaseDTO):
    """Mirror of ResourcePoolSnapshot.

    ``available_count`` is the number of pool members that are not busy at
    snapshot time (live derived from the registry, not a stored count).
    """
    name: str
    member_names: tuple[str, ...]
    available_count: int


class LabwareTemplateDTO(_BaseDTO):
    """Mirror of LabwareTemplateSnapshot.

    Identity-only projection of a labware template (name + type_name).
    Action inputs / declared tracking ride on richer registry DTOs.
    """
    name: str
    type_name: str


class TopologyViewResponse(_BaseDTO):
    """Live composed-topology snapshot.

    Field-equal to the control-plane ``TopologyViewDTO`` so the same wire
    shape parses on either backend. Each list is built from the registry
    facade's snapshot readers.
    """
    devices: list[DeviceDTO] = []
    transporters: list[TransporterDTO] = []
    movers: list[MoverDTO] = []
    resource_pools: list[ResourcePoolDTO] = []
    locations: list[LocationDTO] = []
    labware_templates: list[LabwareTemplateDTO] = []


class TopologySourceResponse(_BaseDTO):
    """Source-of-truth projection (``?source=true``).

    The local daemon is factory-spec backed (not git-file backed), so
    ``source`` carries the ``module:build_topology`` spec the runtime was
    mounted from and the two SHAs are always None.
    """
    source: str
    last_modified_sha: str | None = None
    last_modified_at: str | None = None


class RuntimeStatusResponse(_BaseDTO):
    """Diagnostic snapshot of the runtime lifecycle.

    Field-equal to the control-plane ``RuntimeStatusResponseDTO``.
    ``built`` reflects whether a SystemRuntime is mounted. The daemon has
    no submission pipeline, so ``last_build_error`` is always None.
    """
    built: bool
    last_build_error: dict[str, str] | None = None


class LabwareClearResponse(_BaseDTO):
    """Return shape of the discharge + clear-all runtime mutations."""
    cleared_labware_ids: list[str] = []


class LabwareClearSubmissionResponse(_BaseDTO):
    """Return shape of clear-submission: cleared + surviving reuse-bound."""
    cleared: list[str] = []
    preserved_reuse_bound: list[str] = []


class ParamSpecDTO(BaseModel):
    """Mirror of orca.runtime.danger.ParamSpec."""
    name: str
    type_name: str
    required: bool
    default: str | None
    description: str

    model_config = ConfigDict(extra="forbid")


class CommandDescriptorDTO(BaseModel):
    """Mirror of CommandDescriptor (one capability exposed by a device)."""
    device_name: str
    capability: str
    danger_level: str
    description: str
    cli_accessible: bool
    params: tuple[ParamSpecDTO, ...]

    model_config = ConfigDict(extra="forbid")



class DeviceIntrospectionDTO(_BaseDTO):
    """Mirror of DeviceIntrospection.

    Returned by GET /devices/{name}/introspection. Field-equal to a hosted
    deployment's GET /api/devices/{id}/capabilities response; the CLI's
    `orca device capabilities <id>` subcommand renders this same shape from
    either backend. `name` is the device identity (no separate `device_id`).
    """
    type: str
    name: str
    interfaces: tuple[str, ...]
    capabilities: tuple[str, ...]
    provides_state: bool
    methods: dict[str, dict[str, JsonValue]]


class DeviceInvocationResultDTO(BaseModel):
    """Mirror of DeviceInvocationResult."""
    success: bool
    value_type: str
    value: str | None
    duration_seconds: float
    device_name: str
    command_or_capability: str

    model_config = ConfigDict(extra="forbid")


class DeviceModeEligibilityDTO(BaseModel):
    """Snapshot of which workflow modes a device can run right now.

    Mirrors a hosted REST `ModeEligibility` shape so `orca device
    registry list/show` produces field-equal output regardless of
    backend.
    """

    model_config = ConfigDict(extra="forbid")

    pure_sim: bool
    device_sim: bool
    live: bool


class DeviceRegistryEntryDTO(BaseModel):
    """One device's two-card registry view + live state.

    Mirrors a hosted REST `DeviceRegistryView`. `topology_card` and
    `connection_card` are reused as-is from `orca.runtime.status_models`
    (already Pydantic frozen models). Either may be None: topology-only
    means declared but not connected; connection-only means Q6
    quarantine (reachable via direct dispatch but excluded from
    workflow scheduling).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    topology_card: TopologyCard | None
    connection_card: ConnectionCard | None
    is_client_connected: bool
    """The on-prem client that owns this device is reachable. Per-CLIENT, so it
    says nothing about this device on its own."""
    is_device_connected: bool
    """This device's own link is open, per whoever is driving it: the on-prem
    device bridge when one holds the device, otherwise orca's own dispatch
    driver. Read `device_link_mode` with it, which names the driver that
    answered.
    Without the mode an open DEVICE_SIM or PURE_SIM link reads as the
    instrument, and an open LIVE link reads as the simulator the commands are
    actually reaching. False with no mode means nobody could answer: a device
    bridge advertised this device and is not reporting, so the only thing in
    process is a stand-in holding a stale cache."""
    is_initialized: bool
    """This device has been brought up. Same source and same caveats as
    `is_device_connected`."""
    device_link_mode: EffectiveMode | None
    """Which driver answered the two flags above: the mode the device bridge
    named, or the mode orca's own dispatch resolved when no device bridge holds
    the device. None when nobody could answer. Not the same question as
    `mode_eligibility`, which is what this device COULD run."""
    mode_eligibility: DeviceModeEligibilityDTO
    fault: DeviceFaultDTO | None = None
    """The unresolved fault, if one stands. Every connection flag here reads
    healthy on a faulted device, because the driver behind it is idle and
    answering, so a reader without this concludes the machine is fine."""


class DeviceRegistryListDTO(BaseModel):
    """List response for `GET /devices/registry`."""

    model_config = ConfigDict(extra="forbid")

    devices: list[DeviceRegistryEntryDTO]


class SubmissionDTO(BaseModel):
    """Wire form of Submission.

    ``status`` carries one of ``SubmissionStatus`` -- see that enum's
    docstring for the lifecycle. Forward path:
    ``ACCEPTED -> IN_PROGRESS -> {COMPLETED, FAILED, ABORTED}``.
    Terminal states are sticky.

    ``run_mode`` is the resolved ``WorkflowRunMode`` stamped at submit
    time (PURE_SIM / DEVICE_SIM / LIVE). Required: the runtime always
    populates it via SubmissionSnapshot.
    """
    id: str
    execution_id: str
    workflow_name: str
    group_count: int
    status: str
    batch_mode: str
    submitted_at: str  # ISO-8601
    run_mode: WorkflowRunMode
    operator_id: str | None = None
    deployment_profile: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class LabwareRegisterResponse(BaseModel):
    """POST /labware response."""
    labware: "LabwareDTO"
    status: Literal["registered"] = "registered"

    model_config = ConfigDict(extra="forbid")


class LabwareEditResponse(BaseModel):
    """POST /labware/by-id/{id}/edit-location, reset-location, /barcode response."""
    status: Literal["edited"] = "edited"


class DeviceLifecycleRequest(BaseModel):
    """Optional body for the typed lifecycle routes: which world the verb
    means. Omitted = LIVE, the operator default; the topology sim_override
    ratchet applies on top either way."""
    mode: WorkflowRunMode | None = None


class DeviceInitializeResponse(BaseModel):
    """POST /devices/{name}/initialize response."""
    status: Literal["initialized"] = "initialized"


class DeviceConnectResponse(BaseModel):
    """POST /devices/{name}/connect response."""
    status: Literal["connected"] = "connected"


class DeviceDisconnectResponse(BaseModel):
    """POST /devices/{name}/disconnect response."""
    status: Literal["disconnected"] = "disconnected"


class SubmissionCloseResponse(BaseModel):
    """POST /submissions/{id}/close response (closes the execution batch)."""
    execution_id: str
    phase: str
    status: Literal["closed"] = "closed"


class ResumeAllResultDTO(BaseModel):
    """Return shape of `SystemRuntime.resume_all_threads`.

    Keys match the runtime's own dict:

    * ``resumed`` -- threads that were PAUSED and are now running.
    * ``pause_cancelled`` -- threads that had a pending pause request
      queued by an earlier ``pause_all_threads`` but had not yet
      reached PAUSED; their queued pause is cancelled so they keep
      running instead of latching into PAUSED later. Closes the
      pause + resume footgun where a queued pause kept firing on
      threads that reached the checkpoint after the resume call.
    * ``error_skipped`` -- threads that were PAUSED with an error
      (need explicit ``recover``).
    * ``completed_skipped`` -- threads that terminated between the
      status check and the resume attempt (TOCTOU window).
    """
    resumed: int
    pause_cancelled: int = 0
    error_skipped: int
    completed_skipped: int



class PauseAllResultDTO(BaseModel):
    """Return shape of `SystemRuntime.pause_all_threads`.

    Mirrors ``ResumeAllResultDTO``: counters tell the operator how many
    threads were actually pause-eligible. ``pausing == 0`` typically
    means the execution is terminal -- previously the endpoint returned
    a generic ``"pausing"`` status that hid that no-op.
    """
    pausing: int
    already_paused: int
    terminal_skipped: int
