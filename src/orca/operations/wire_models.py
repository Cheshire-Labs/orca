"""Pydantic wire models shared by the REST routes and the CLI.

This satisfies the "JSON = Pydantic" rule and gives clients a stable
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
from cheshire_drivers.driver_introspection import MethodInfo
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    field_serializer,
)

from orca.operations.blocker_models import BlockerDTO
from orca.operations.thread_models import ActionSnapshotModel, MethodSnapshotModel
from orca.runtime.execution_record import ExecutionState
from orca.runtime.incident_store import SystemIncident
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode
from orca.state.records import LabwareInitialState
from orca.runtime.status_models import ConnectionCard, TopologyCard
from orca.runtime.runtime_state import RuntimeState
from orca.variables.resolution import VariableResolution, VariableSource
from orca.workflow_models.status_enums import FailurePolicy


# -- Shared enums ------------------------------------------------------------


class OperationResult(Enum):
    """Wire-level enum for action-route success responses.

    `POST /shutdown` and `POST /api/runtime/unload` return `{status: <OperationResult.value>}`.
    """
    STOPPED = "stopped"
    UNLOADED = "unloaded"


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
RunModeStr = Literal["CHECK_RUN", "SIM", "LIVE"]

# Tuple form for runtime membership checks; the source of truth for
# valid wire-level run-mode strings, used by CLI input narrowing.
_VALID_RUN_MODES: tuple[RunModeStr, ...] = ("CHECK_RUN", "SIM", "LIVE")


def is_run_mode_str(value: str) -> TypeGuard[RunModeStr]:
    """``TypeGuard`` narrowing an arbitrary ``str`` to ``RunModeStr``.

    Used by CLI verbs that accept ``--run-mode`` as an unbounded string
    and need to narrow to the Literal before passing through to clients
    whose signatures are typed ``RunModeStr | None``. The runtime check
    plus the TypeGuard is the project-blessed alternative to ``cast``.
    """
    return value in _VALID_RUN_MODES


class AcquisitionDTO(BaseModel):
    """Discriminated-union form for Acquisition.

    `kind`: "pool" -> no further fields; "barcode" -> barcode; "location"
    -> source_location. verify_barcode is refused: nothing checks it yet.
    """
    kind: Literal["pool", "barcode", "location"] = "pool"
    barcode: str | None = None
    source_location: str | None = None
    verify_barcode: str | None = None


class LabwareGroupMemberDTO(BaseModel):
    """Wire form of LabwareGroupMember."""
    thread_template_name: str
    acquisition: AcquisitionDTO


class LabwareGroupDTO(BaseModel):
    """Wire form of LabwareGroup."""
    id: str
    members: list[LabwareGroupMemberDTO]
    name: str | None = None


class MountPackageRequest(BaseModel):
    """POST /api/runtime/mount-package body. The daemon builds its package folder; each submission picks its own run mode."""
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- Lifecycle response DTOs -------------------------------------------------


class HealthResponse(BaseModel):
    """GET /health body, the same on every server. Always 200 while the server is up.

    `system_loaded` says whether a runtime is built and `runtime_state` what state it is in. `package_dir` is the
    folder the server builds from. `mounting` is true while a build or rebuild is still running, so a caller whose
    request timed out can see that before retrying.
    """
    status: Literal["ok"] = "ok"
    system_loaded: bool
    runtime_state: RuntimeState | None
    package_dir: str
    mounting: bool = False


class MountPackageResponse(BaseModel):
    """POST /api/runtime/mount-package success body: the folder built and the post-mount RuntimeState."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    package_dir: str
    runtime_state: RuntimeState


class ReloadRequest(BaseModel):
    """`reason` is written on the reload's audit row."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(default="")


class ReloadResponse(BaseModel):
    """What a reload registered. `head_sha` is the package revision, on a server that tracks one."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str | None
    registered: list[str]
    removed: list[str]
    unchanged: list[str]


# -- Action-result response DTOs --------------------------------------------
#
# `POST /shutdown` and `POST /api/runtime/unload` return {status: <OperationResult>}.


class _ActionResponse(_BaseDTO):
    """Action responses share one field. Concrete subclasses pin a default."""
    status: OperationResult


class ShutdownResponse(_ActionResponse):
    """POST /shutdown success body."""
    status: OperationResult = OperationResult.STOPPED


class UnloadResponse(_ActionResponse):
    """POST /api/runtime/unload success body."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: OperationResult = OperationResult.UNLOADED


class AuditEntryDTO(BaseModel):
    """One confirmed audited operation, as the server's audit store holds it."""
    timestamp: float
    action_name: str
    danger_level: str
    reason: str | None
    call_args: dict[str, JsonValue]


class ReservationCancelResponse(BaseModel):
    """DELETE /api/executions/{id}/reservations/{rsv_id} response."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["cancelled"] = "cancelled"


# -- Dataclass-mirror response DTOs -----------------------------------------


class ThreadSnapshotResponse(_BaseDTO):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    name: str
    status: str
    current_location: str | None
    current_method: MethodSnapshotModel | None
    completed_method_count: int
    last_error: str | None
    pause_reason: str | None
    completed_methods: tuple[str, ...]
    labware_template_name: str | None = None
    labware_id: str | None = None
    labware_name: str | None = None
    group_id: str | None = None
    """The labware group this thread serves; null for a thread shared across groups or a submission sent without groups."""
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
    # Populated by the cross-execution ``GET /api/reservations`` route. The
    # per-execution route leaves it null: the URL already pins the scope.
    execution_id: str | None = None
    # What the hold is for, and what kind of hold it is: a person being asked
    # to place that labware, or that labware on its way here. Neither, and it
    # is a hold that rests nothing at this position.
    labware_name: str | None = None
    awaiting_operator: bool = False
    arriving: bool = False


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
    labware_type: str
    placed_before_run: bool
    initial_state: LabwareInitialState | None


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


class IncidentResponse(BaseModel):
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

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    """POST /api/incidents/{id}/ack and POST /api/incidents/ack-all response."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    acknowledged_count: int


class RecoverableTimeoutDecisionResponse(BaseModel):
    """Response for the three recoverable-timeout decision endpoints."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    incident_id: str
    decision: str
    record_left_as_it_is: bool = False
    """True when mark complete changed nothing in the record: the labware is no longer where the command started
    from, because a retry or a later move already put it somewhere else."""


class PluginDTO(_BaseDTO):
    """Mirror of PluginSnapshot."""
    type_name: str


class TeachpointDTO(BaseModel):
    """Mirror of cheshire_drivers.teachpoints.Teachpoint, scoped per-device.

    `position_id` is the world-relative deck-slot identifier this teachpoint
    targets. `coord_type` is the cartesian/joint discriminator; `coords` is
    the flat wire dict of axis values only (no `type`/`orientation` keys, which
    ride at the top level) so CLI clients render coordinates without
    reconstructing typed coordinate models.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

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


class DeviceFaultModel(BaseModel):
    """Mirror of ``DeviceFaultSummary``.

    ``outcome`` is ``failed`` when the driver reported the failure itself and
    ``unknown`` when no answer came back. On ``unknown`` the command may still
    be running on the instrument, which is what ``may_still_be_moving`` says.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    command: str
    outcome: Literal["failed", "unknown"]
    error: str
    error_type: str
    at: float
    may_still_be_moving: bool
    message: str
    execution_id: str | None = None


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

    `type_name` is the template class; `labware_type` is the catalog key. A sim
    template's placeholder, such as `_sim_plate`, is not in the catalog.
    """
    name: str
    type_name: str = Field(description="The template's Python class, e.g. `PlateTemplate`.")
    labware_type: str = Field(description=(
        "The catalog key, the one `GET /api/labware-catalog/{labware_type}` takes. A sim "
        "template's placeholder, such as `_sim_plate`, is not in the catalog."
    ))


class TopologyViewResponse(_BaseDTO):
    """The composed topology. Devices are what topology.py declares; the device registry has their live state."""
    devices: list[TopologyCard] = []
    transporters: list[TransporterDTO] = []
    movers: list[MoverDTO] = []
    resource_pools: list[ResourcePoolDTO] = []
    locations: list[LocationDTO] = []
    labware_templates: list[LabwareTemplateDTO] = []


class TopologySourceResponse(_BaseDTO):
    """Source-of-truth projection (``?source=true``).

    The local daemon has no git history, so ``source`` is its package's
    ``topology.py`` and the two SHAs are always None.
    """
    source: str
    last_modified_sha: str | None = None
    last_modified_at: str | None = None


class RuntimeStatusResponse(_BaseDTO):
    """Whether the run can proceed, and everything standing in its way.

    It answers on a server whose runtime never built. An empty `blockers` list
    is the only thing that means the run can move.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    built: bool = Field(..., description="True if SystemRuntime is live.")
    last_build_error: dict[str, str] | None = Field(
        default=None,
        description=(
            "When built=False, the most recent build failure: type, message and hint. "
            "Null when built=True or no failure has been recorded."
        ),
    )
    blockers: list[BlockerDTO] = Field(
        default_factory=list,
        description="Everything stopping the run, worst first, with the remedies that clear it.",
    )
    blocker_count: int = 0
    blockers_known: bool = Field(
        default=True,
        description=(
            "False when nobody could read the blockers. An empty list with this false "
            "means the answer is unknown; do not report the run as free to move."
        ),
    )
    blockers_fingerprint: str = Field(
        default="",
        description="Changes when the blocker set changes.",
    )


class DeviceIntrospectionDTO(_BaseDTO):
    """What a device can run: GET /api/devices/{device_id}/introspection on both servers.

    A connected device answers with what its orca-client advertised at the handshake; otherwise the
    topology's driver answers. `name` is the device identity.
    """
    type: str
    name: str
    interfaces: tuple[str, ...]
    capabilities: tuple[str, ...]
    provides_state: bool
    methods: dict[str, MethodInfo]
    danger_levels: dict[str, str]
    """Each command's danger level, keyed as `methods` is: the name a command is sent by."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DeviceModeEligibilityDTO(BaseModel):
    """Snapshot of which workflow modes a device can run right now.

    Mirrors a hosted REST `ModeEligibility` shape so `orca device list`
    and `orca device registry show` produce field-equal output regardless
    of backend.
    """

    model_config = ConfigDict(extra="forbid")

    check_run: bool
    sim: bool
    live: bool


class EngineDeviceStateDTO(_BaseDTO):
    """Mirror of EngineDeviceState."""
    type_name: str
    is_busy: bool
    effective_mode: WorkflowRunMode
    setting: DeviceMode
    position_ids: tuple[str, ...]
    loaded_labware_ids: tuple[str, ...]
    held_labware_id: str | None
    under_external_control: bool
    external_control_hold: str | None
    bring_up_error: str | None = None


class DeviceRegistryEntryResponse(BaseModel):
    """One device's two-card registry view + live state.

    Mirrors a hosted REST `DeviceRegistryView`. `topology_card` and
    `connection_card` are reused as-is from `orca.runtime.status_models`
    (already Pydantic frozen models). Either may be None: topology-only
    means declared but not connected; connection-only means Q6
    quarantine (reachable via direct dispatch but excluded from
    workflow scheduling).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    Without the mode an open SIM or CHECK_RUN link reads as the
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
    fault: DeviceFaultModel | None = None
    """The unresolved fault, if one stands. Every connection flag here reads
    healthy on a faulted device, because the driver behind it is idle and
    answering, so a reader without this concludes the machine is fine."""
    engine: EngineDeviceStateDTO | None = None
    """What the engine holds for this device: busy, labware, what a transporter
    holds, external control. None for a device only the on-prem client reports."""
    interface_mismatch: str | None = None
    """Set on a list row whose connection advertises fewer interfaces than topology.py
    declares. The one-device read refuses such a device instead."""


class DeviceRegistryListResponse(BaseModel):
    """List response for `GET /api/devices`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    devices: list[DeviceRegistryEntryResponse]


class SubmissionDTO(BaseModel):
    """Wire form of Submission.

    ``status`` carries one of ``SubmissionStatus`` -- see that enum's
    docstring for the lifecycle. Forward path:
    ``ACCEPTED -> IN_PROGRESS -> {COMPLETED, PARTIAL_COMPLETED, FAILED, ABORTED}``.
    Terminal states are sticky.

    ``run_mode`` is the resolved ``WorkflowRunMode`` stamped at submit
    time (CHECK_RUN / SIM / LIVE). Required: the runtime always
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
