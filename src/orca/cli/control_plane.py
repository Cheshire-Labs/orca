"""orca's control-plane contract: the Protocol + DTOs that any backend
implementation MUST conform to so the orca CLI can target it.

This module declares the source-available boundary contract. Backends (loopback
daemon, commercial cloud deployments, future alternatives) implement
the ``IControlPlaneClient`` Protocol and conform to the DTO shapes
declared here. Orca-core OWNS the wire contract; backend implementers
conform to it.

In-tree implementations today:

* ``LocalDaemonClient`` (loopback orca-core daemon over ``127.0.0.1``;
  trust = "the user can already run code on this machine"; no auth).
* A cloud backend (REST over the network with an ``X-API-Key`` header;
  trust = deployment-secret-bearing operator on a hosted deployment).
  It lives behind the ``orca.cli_backends`` entry point, so a build that
  ships none still installs and runs every local verb.

Same ``orca <command>`` CLI surface targets either via this protocol;
cloud-mode users get the smaller overlapping subset, daemon-mode users
get the full surface.

DTOs in this module are the **wire shape consumed by CLI verbs** -- distinct
from the daemon's ``orca.daemon.schemas`` (which mirror runtime dataclasses
with their full field set, and use the ``ExecutionState`` enum on ``status``).
Every status field on this Protocol is ``str``, not the enum, so the existing
``_TERMINAL_STATES`` string check in ``orca run --wait`` works against any
conforming backend.
"""

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Protocol, runtime_checkable

from cheshire_drivers.gateway_protocol import EffectiveMode
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from orca.runtime.run_modes import WorkflowRunMode
from orca.operations.manual_step_models import PendingManualStepDTO
from orca.operations.state_models import UnsettledStateResponse
from orca.operations.submission_models import SubmitExecutionResponse
from orca.daemon.schemas import (
    AccessConfigDTO,
    GripProfileDTO,
    GripProfilePatchRequest,
    IncidentAckResponse,
    IncidentDTO,
    RecoverableTimeoutDecisionResponse,
    ResumeAllResultDTO,
    MoveDefaultsDTO,
    MoveDefaultsPatchRequest,
    CommandDescriptorDTO,
    CrossExecVariableDTO,
    DeckLayoutDTO,
    DeckLayoutSummaryDTO,
    DeviceDTO as DaemonDeviceDTO,
    DeviceFaultDTO,
    DeviceInvocationResultDTO,
    LabwareDTO,
    LabwareTemplateDTO,
    LocationDTO,
    MoverDTO,
    LocationEventDTO,
    OptionValueJson,
    PluginCommandDTO,
    PluginDTO,
    ReservationSnapshotDTO,
    ResourcePoolDTO,
    RunModeStr,
    SubmissionDTO,
    SubmissionSubmitRequest,
    SystemInfoDTO,
    TeachpointDTO,
    ThreadSnapshotDTO as DaemonThreadSnapshotDTO,
    ThreadTemplateDTO,
    TransporterDTO,
    VariableResolutionResponse,
    VariableSetResponse,
    VariableValue,
)
from orca.operations.device_models import (
    ClearDeviceFaultResponse,
    CompareDeckResponse,
    GetMountedTipsResponse,
    MountedTipDTO,
    ReconcileDeckResponse,
)
from orca.operations.labware_models import (
    GetTipStateResponse,
    GetWellVolumesResponse,
    MarkTipsUsedResponse,
    ResolveContentsResponse,
)
from orca.operations.thread_models import InsertWhere, ReplaceResult
from orca.state.records import TrackingRecord


class _CPModel(BaseModel):
    """Base for control-plane DTOs.

    ``extra='ignore'`` (NOT 'forbid') because backend implementations may
    grow new wire fields ahead of the Protocol contract. Forbidding extras
    would break the CLI path on every backend-side field addition; ignoring
    keeps the Protocol decoupled from backend-side wire churn.
    """

    model_config = ConfigDict(extra="ignore")


class ExecutionRecordDTO(_CPModel):
    """Trim execution record returned by ``submit_workflow`` and ``list_executions``.

    Orca's CLI contract. ``status`` is a string, not an enum; ``LocalDaemonClient``
    converts the daemon's ``ExecutionState`` enum on parse, and cloud
    backends conform to the same string shape on the wire.
    """

    id: str
    workflow_name: str
    status: str
    error: str | None = None
    paused: bool = False
    """Whether the execution-level pause latch is set. Separate from
    ``status``, which a pause never changes: a paused run still reports
    `accepting` or `draining`."""
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort."""


class _CPSubmissionDTO(_CPModel):
    """Cloud-only submission record wire shape.

    Cloud backends' ``POST /api/executions`` (or equivalent) returns the
    full submission record (nine fields), not the trim execution
    record. This mirror parses that payload; the cloud backend's
    ``submit_workflow`` then projects it into the Protocol's
    ``ExecutionRecordDTO`` so callers keep reading ``.id`` as the
    EXECUTION id (NOT the submission id). The submission id is dropped
    at the CLI boundary; it is a backend-side implementation detail that
    orca's verbs do not surface.

    ``_CPModel.extra='ignore'`` keeps the cloud path resilient to
    additional backend-side wire fields.
    """

    id: str
    execution_id: str
    workflow_name: str
    group_count: int
    status: str
    batch_mode: str
    submitted_at: str
    run_mode: str | None = None
    operator_id: str | None = None
    deployment_profile: str | None = None


class ThreadSnapshotDTO(_CPModel):
    """Per-thread snapshot embedded in `ExecutionDetailDTO.threads`.

    Field-equal to daemon's `orca.daemon.schemas.ThreadSnapshotDTO`. Cloud
    backend defaults missing fields to `None` / empty.
    """

    id: str
    name: str
    status: str
    current_location: str = ""
    current_method: dict[str, JsonValue] | None = None
    completed_method_count: int = 0
    last_error: str | None = None
    pause_reason: str | None = None
    completed_methods: tuple[str, ...] = ()
    labware_template_name: str | None = None
    labware_id: str | None = None
    labware_name: str | None = None
    paused_device_command: str | None = None
    pause_message: str | None = None
    pause_site: str | None = None
    honoured_decisions: tuple[str, ...] = ()
    waiting_for: str | None = None


class ExecutionDetailDTO(_CPModel):
    """Rich execution detail returned by ``get_execution``.

    Cloud backends MUST hit the detail endpoint (e.g.
    ``POST /api/operations/get-execution-detail``), NOT the trim
    ``get-execution``, to populate the thread fields; fields the
    backend runtime does not expose default to ``None`` / empty.
    """

    id: str
    workflow_name: str
    status: str
    error: str | None = None
    threads: list[ThreadSnapshotDTO] = []
    total_thread_count: int = 0
    completed_thread_count: int = 0
    active_thread_count: int = 0
    paused: bool = False
    """Whether the execution-level pause latch is set. Separate from
    ``status``, which a pause never changes: a paused run still reports
    `accepting` or `draining`."""
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort."""


class MoverHoldReleaseDTO(_CPModel):
    """What a mover-hold release did.

    ``released_to`` names where the operator said the labware is; it is None
    exactly when ``discharged`` is True.
    """

    mover_name: str
    labware_id: str
    labware_name: str
    released_to: str | None = None
    discharged: bool = False


class ExecutionCloseResponseDTO(_CPModel):
    """Return shape of `execution close` (ACCEPTING -> DRAINING).

    Field-equal to the daemon's `SubmissionCloseResponse` and the cloud's
    `CloseExecutionResponse`: both carry `execution_id` + `phase`. The
    daemon adds a constant `status="closed"`; cloud omits it, so it is
    optional here.
    """

    execution_id: str
    phase: str
    status: str = "closed"


class StopExecutionResultDTO(_CPModel):
    """Return shape of `execution stop` -- a two-call confirmed abort.

    ``status`` is ``"armed"`` (first/cold call: paused + armed, NOT aborted) or
    ``"aborted"`` (confirmed call on an armed execution). ``phase`` is the
    ExecutionPhase string after the request; ``message`` is operator-facing.
    Field-equal to the daemon's and the cloud's ``StopExecutionResponse``.
    """

    execution_id: str
    status: str
    phase: str
    message: str


class DeviceDTO(_CPModel):
    """Minimal device summary used by `list_devices`.

    Just ``name`` + ``type_name`` -- the lowest-common-denominator across
    daemon (rich runtime state) and cloud backends (websocket-handshake
    registry shape). Daemon-only consumers can call
    ``LocalDaemonClient.device_info(name)`` for the rich ``DeviceSnapshot``.
    """

    name: str
    type_name: str


class AuditEntryDTO(_CPModel):
    """One @dangerous-confirmed runtime operation as recorded in the daemon's
    in-memory audit ring buffer (durable mirror at log_dir/orca_audit.log).

    `call_args` is loosely typed because @dangerous captures the bound kwargs
    of arbitrary actions. Consumers that need typed access render specific
    keys per action_name.
    """

    timestamp: float
    action_name: str
    danger_level: str
    reason: str | None = None
    call_args: dict[str, JsonValue] = {}


class DeviceIntrospectionDTO(_CPModel):
    """Driver introspection payload.

    Same shape on both backends:
    * Daemon: `GET /devices/{name}/introspection`.
    * Hosted deployment: `GET /api/devices/{id}/capabilities`.

    Backs the `orca device capabilities <id>` subcommand. `name` is the
    device identity on both surfaces; there is no separate `device_id`
    (the cloud's `DeviceCapabilitiesResponse` never emitted one, and the
    daemon's was a redundant alias of `name`). Coexists with the
    `orca device info --capabilities` flag: different content; the
    flag calls daemon's `GET /devices/{name}/capabilities` for per-command
    descriptors.
    """

    type: str | None = None
    name: str
    interfaces: list[str] = []
    capabilities: list[str] = []
    provides_state: bool = False
    methods: dict[str, dict[str, JsonValue]] = {}


class DeviceModeEligibilityDTO(_CPModel):
    """Snapshot of which workflow modes a device can run right now.

    Field-equal across backends. ``extra='ignore'`` per ``_CPModel`` so
    backend implementations can grow new mode kinds without breaking the
    cloud read path.
    """

    pure_sim: bool
    device_sim: bool
    live: bool


class DeviceRegistryEntryDTO(_CPModel):
    """One device's two-card registry view + live state.

    Backs `orca device registry list/show` against either backend:
    * Daemon: `GET /devices/registry` and `/devices/registry/{name}`.
    * Hosted deployment: `GET /api/devices/registry` and
      `/api/devices/registry/{name}`.

    `topology_card` and `connection_card` are intentionally `dict | None`
    on this Protocol surface (NOT the typed Pydantic models from
    `orca.runtime.status_models`) because cloud responses may carry
    fields the orca-core models do not declare; CLI rendering only
    needs name + raw values for display.
    """

    name: str
    topology_card: dict[str, JsonValue] | None = None
    connection_card: dict[str, JsonValue] | None = None
    is_client_connected: bool = False
    is_device_connected: bool = False
    is_initialized: bool = False
    # Which driver answered the two flags above. A backend that does not send
    # it leaves this None and the renderer falls back to the connection card,
    # which carries the device bridge's copy of the same value.
    device_link_mode: EffectiveMode | None = None
    mode_eligibility: DeviceModeEligibilityDTO = DeviceModeEligibilityDTO(
        pure_sim=False, device_sim=False, live=False,
    )
    # Every connection flag above reads healthy on a faulted device, because
    # the driver behind it is idle and answering.
    fault: DeviceFaultDTO | None = None


# -- Cloud-only DTOs (orca's contract for cloud backend wire shapes) ------
#
# These DTOs declare the wire shape orca's CLI consumes from cloud
# backend implementations. A cloud backend MUST
# conform to these shapes. ``extra='ignore'`` on ``_CPModel`` keeps the
# CLI path resilient to additional fields a backend may send ahead of
# contract evolution.


class SubmitModuleResponseDTO(_CPModel):
    """Wire contract for cloud-side ``SubmitModuleResponse``.

    Returned by every typed submission write surface
    (``POST /api/workflows``, ``POST /api/topology``, and the workflow
    delete route). ``kind`` is one of the literal values
    (``"topology" | "method" | "workflow" | "plugin"``); kept as ``str``
    on this Protocol surface because backend implementations may extend
    the kind set ahead of the CLI.
    """

    commit_sha: str
    committed_at: str
    kind: str
    name: str


class GitFileEntryDTO(_CPModel):
    """Wire contract for cloud-side ``GitFileEntry`` (one path at HEAD)."""

    path: str
    kind: str | None = None
    name: str | None = None
    last_modified_sha: str
    last_modified_at: str


class GitFileSourceDTO(_CPModel):
    """Wire contract for cloud-side ``GitFileSource`` (one file's source + metadata)."""

    path: str
    kind: str | None = None
    name: str | None = None
    source: str
    last_modified_sha: str
    last_modified_at: str


class WorktreeFileEntryDTO(_CPModel):
    """Wire contract for cloud-side ``WorktreeFileEntry`` (one on-disk file).

    Sister of GitFileEntryDTO: same path-decoded shape, but the metadata
    is on-disk (size + mtime) rather than commit-derived. Describes what
    ``runtime_reload`` will read, which can diverge from HEAD when files
    have been submitted but not committed.
    """

    path: str
    kind: str | None = None
    name: str | None = None
    size_bytes: int
    modified_at: str


class WorktreeFileSourceDTO(_CPModel):
    """Wire contract for cloud-side ``WorktreeFileSource`` (one on-disk file's source)."""

    path: str
    kind: str | None = None
    name: str | None = None
    source: str
    size_bytes: int
    modified_at: str


class WorkflowSummaryDTO(_CPModel):
    """Wire contract for cloud-side ``WorkflowSummary`` (aliased to ``WorkflowTemplateDTO``).

    Wire shape uses a JSON array for ``entry_thread_template_names``; we
    accept ``list[str]`` here because backends serialize the canonical
    tuple-typed field as a JSON list.
    """

    name: str
    entry_thread_template_names: list[str] = []


class MethodSummaryDTO(_CPModel):
    """Wire contract for cloud-side ``MethodSummary`` (aliased to ``MethodTemplateDTO``).

    ``failure_policy`` rides as its ``.name`` string on the wire; kept
    typed as ``str`` here so the CLI does not need to import the enum.
    """

    workflow_name: str
    name: str
    failure_policy: str


class TopologyViewDTO(_CPModel):
    """Wire contract for cloud-side ``TopologyView`` live composed topology shape.

    The entry types are the canonical orca-core daemon DTOs. Cloud
    backends import those same types from orca-core, so the wire shape
    is structurally identical across backends.
    """

    devices: list[DaemonDeviceDTO] = []
    transporters: list[TransporterDTO] = []
    movers: list[MoverDTO] = []
    resource_pools: list[ResourcePoolDTO] = []
    locations: list[LocationDTO] = []
    labware_templates: list[LabwareTemplateDTO] = []


class TopologySourceDTO(_CPModel):
    """Wire contract for cloud-side ``TopologySource`` (?source=true projection)."""

    source: str
    last_modified_sha: str | None = None
    last_modified_at: str | None = None


class JourneyMoveDTO(_CPModel):
    """Wire contract for cloud-side ``JourneyMove`` entry in a labware's journey."""

    kind: Literal["move"] = "move"
    sequence: int
    position_id: str
    timestamp: float


class JourneyActionDTO(_CPModel):
    """Wire contract for cloud-side ``JourneyAction`` entry in a labware's journey.

    ``details`` is the discriminated ``OperationDetails`` union on the
    backend side; this Protocol DTO keeps it as ``dict[str, JsonValue]``
    to avoid pulling in cheshire-drivers types (those live in
    cheshire-drivers, not orca-core).
    """

    kind: Literal["action"] = "action"
    timestamp: float
    device_name: str
    operation: str
    details: dict[str, JsonValue] = {}
    execution_id: str
    thread_id: str
    action_id: str
    # Mirrors JourneyAction.method_id (str | None): None for bootstrap
    # initial-state seeds and free-floating actions outside any method.
    method_id: str | None


JourneyEntryDTO = Annotated[
    JourneyMoveDTO | JourneyActionDTO, Field(discriminator="kind"),
]


class LabwareJourneyResponseDTO(_CPModel):
    """Wire contract for cloud-side ``LabwareJourneyResponse`` (moves + actions merge)."""

    labware_id: str
    entries: list[JourneyEntryDTO] = []


class LabwareCatalogEntryDTO(_CPModel):
    """Wire contract for cloud-side ``LabwareCatalogEntry``.

    One row in the backend's ``labware_definitions`` store projected
    onto the CLI surface. ``geometry`` carries the orca-core / PLR
    converter shape; the CLI does not introspect it.
    """

    labware_type: str
    display_name: str
    category: str
    vendor: str | None = None
    source: str
    geometry: dict[str, JsonValue] = {}
    plr_class_name: str | None = None


class LabwareCatalogSummaryDTO(_CPModel):
    """Wire contract for one catalog row WITHOUT geometry (the list shape).

    ``GET /labware`` and ``GET /api/labware`` (and the ``catalog_labware``
    MCP tool) return these. Geometry blobs run to ~77 KB per 384-well plate,
    so the list omits them; fetch geometry per row via ``get_labware``.
    """

    labware_type: str
    display_name: str
    category: str
    vendor: str | None = None
    source: str
    plr_class_name: str | None = None


class LabwareCatalogResponseDTO(_CPModel):
    """Wire contract for the catalog list response (geometry-free rows).

    ``GET /api/labware`` (and the ``catalog_labware`` MCP tool) return
    a flat list of summary rows. The legacy
    ``{"carriers": {...}, "labware": {...}}`` shape was retired; carriers
    now appear in the same list with ``category='carrier'``.
    """

    labware: list[LabwareCatalogSummaryDTO] = []


class LabwareClearResponseDTO(_CPModel):
    """Wire contract for the daemon's ``LabwareClearResponse`` (discharge + clear-all only).

    `clear-submission` returns the richer
    :class:`LabwareClearSubmissionResponseDTO` shape that ALSO surfaces
    `preserved_reuse_bound`. A cloud deployment answers these three in the
    operation models' own shapes, which its backend reads directly.
    """

    cleared_labware_ids: list[str] = []


class LabwareClearSubmissionResponseDTO(_CPModel):
    """Wire contract for the daemon's ``LabwareClearSubmissionResponse``.

    ``POST /labware/runtime/clear-submission`` returns both lists so the
    operator sees what survived (reuse-bound / deck-resident labware
    skipped because the thread template declared ``end_leave_in_place``).
    """

    cleared: list[str] = []
    preserved_reuse_bound: list[str] = []


class OpsHistoryGetResponseDTO(_CPModel):
    """Wire contract for cloud-side ``ExecutionOpsHistoryDTO`` (per-execution archive).

    ``records`` carries canonical ``TrackingRecord`` instances; that
    type is owned by orca-core (``orca.state.records``) and
    is therefore safe to import directly here.
    """

    execution_id: str
    records: list[TrackingRecord] = []


class OpsHistorySearchResponseDTO(_CPModel):
    """Wire contract for cloud-side ``OpsHistorySearchResultDTO`` (cross-execution search).

    Each ``TrackingRecord`` self-describes its owning execution via the
    required ``execution_id`` field.
    """

    records: list[TrackingRecord] = []


class ReloadResponseDTO(_CPModel):
    """Wire contract for cloud-side ``ReloadResponse``.

    Returned by both ``POST /api/runtime/reload`` (full rebuild from
    HEAD) and ``POST /api/runtime/reload_workflow`` (workflows-only
    reconcile).
    """

    head_sha: str | None = None
    registered: list[str] = []
    removed: list[str] = []
    unchanged: list[str] = []


class RemedyStepDTO(_CPModel):
    """One call in a remedy, named on every surface."""

    verb: str
    label: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    mcp_tool: str = ""
    rest: str = ""
    cli: str = ""
    needs: list[str] = Field(default_factory=list)
    query: list[str] = Field(default_factory=list)


class RemedyDTO(_CPModel):
    """A named recovery: what it does, and the steps in order."""

    id: str
    label: str
    explain: str = ""
    steps: list[RemedyStepDTO] = Field(default_factory=list)
    recommended: bool = False
    confirm: bool = False

    def cli_line(self) -> str:
        """The commands to run, in order, as an operator would type them."""
        commands = [step.cli for step in self.steps if step.cli]
        if not commands:
            return self.label
        return " && ".join(commands)


class BlockerDTO(_CPModel):
    """One thing stopping the run, with the remedies that clear it."""

    id: str
    kind: str
    severity: str
    since: float | None = None
    headline: str
    remedies: list[RemedyDTO] = Field(default_factory=list)
    detail: str | None = None
    device_name: str | None = None
    execution_id: str | None = None
    thread_id: str | None = None
    labware_id: str | None = None
    incident_id: str | None = None
    may_still_be_moving: bool = False
    blocks: list[str] = Field(default_factory=list)


class RuntimeStatusResponseDTO(_CPModel):
    """Wire contract for cloud-side ``RuntimeStatusResponse``.

    Returned by ``GET /api/runtime/status``. ``built`` says whether the
    SystemRuntime is live; ``last_build_error`` carries the most recent
    build/rebuild failure type, message, and a recovery hint when
    ``built`` is False. ``blockers`` is everything stopping the run,
    worst first; empty is the only thing that means nothing is in the way,
    and only while ``blockers_known`` is true.
    """

    built: bool
    last_build_error: dict[str, str] | None = None
    blockers: list[BlockerDTO] = Field(default_factory=list)
    blocker_count: int = 0
    blockers_fingerprint: str = ""
    blockers_known: bool = False
    """False by default: a deployment that never sent the field has not said
    the way is clear, and this model ignores extras precisely because
    backends drift."""


class ControlPlaneError(Exception):
    """Raised by either backend client when the call cannot complete.

    The CLI turns it into an error message and an exit code: `exit_code` when
    set, else `output.exit_code_for_status(http_status)`. A request that got
    no answer has no status, so it sets `exit_code` instead.
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
    ) -> None:
        super().__init__(message)
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
    """The overlapping command surface every backend MUST implement.

    Orca's source-available boundary contract. Every method here is a CLI
    command that works on both backends. Local-only methods live on
    ``LocalDaemonClient`` only; cloud-only methods live on whichever class is
    registered for entry-point group ``orca.cli_backends`` name ``cloud``. The
    dispatch layer in ``orca.cli.app`` uses a per-command classification tag to
    fail-clean symmetrically: cloud-only on ``local`` exits non-zero, and
    local-only on ``cloud`` exits non-zero.

    Note: ``@runtime_checkable`` only verifies method-name presence per
    CPython typing. A sibling ``inspect.signature`` comparison test
    is the actual signature gate alongside pyright.
    """

    def submit_workflow(
        self,
        workflow_name: str,
        variables: Mapping[str, object] | None = None,
        *,
        run_mode: RunModeStr,
        acknowledge_warnings: bool = False,
    ) -> ExecutionRecordDTO: ...

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
    ) -> ExecutionRecordDTO: ...

    def list_executions(self) -> Sequence[ExecutionRecordDTO]: ...

    def get_execution(self, execution_id: str) -> ExecutionDetailDTO: ...

    def stop_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> StopExecutionResultDTO: ...

    def execution_remove(self, execution_id: str) -> None: ...

    def list_devices(self) -> Sequence[DeviceDTO]: ...

    def device_info(self, device_name: str) -> DaemonDeviceDTO: ...

    def list_device_snapshots(self) -> Sequence[DaemonDeviceDTO]: ...

    def device_introspection(self, device_id: str) -> DeviceIntrospectionDTO: ...

    def device_registry_list(self) -> Sequence[DeviceRegistryEntryDTO]: ...

    def device_registry_show(self, device_id: str) -> DeviceRegistryEntryDTO: ...

    def device_get_mounted_tips(self, device_name: str) -> GetMountedTipsResponse: ...

    def device_set_mounted_tips(
        self, device_name: str, mounted: list[MountedTipDTO],
        *, reason: str | None = None,
    ) -> None: ...

    def device_confirm_mounted_tips(
        self, device_name: str, *, reason: str | None = None,
    ) -> None: ...

    def device_clear_fault(self, device_name: str) -> ClearDeviceFaultResponse: ...

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

    def thread_abort_method(
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

    # -- Variables -------------------------------------------------------

    def variables_list(self, execution_id: str) -> dict[str, OptionValueJson]: ...

    def variables_get(
        self, execution_id: str, name: str, submission_id: str | None = None,
    ) -> VariableValue: ...

    def variables_resolution(
        self, execution_id: str, name: str,
    ) -> VariableResolutionResponse: ...

    def variables_set(
        self, execution_id: str, name: str, value: OptionValueJson,
    ) -> VariableSetResponse: ...

    def variables_set_submission(
        self, execution_id: str, submission_id: str, name: str,
        value: OptionValueJson,
    ) -> None: ...

    def variables_set_global(self, name: str, value: OptionValueJson) -> None: ...

    def variables_unset(self, execution_id: str, name: str) -> None: ...

    def variables_unset_submission(
        self, execution_id: str, submission_id: str, name: str,
    ) -> None: ...

    def variables_list_all(self) -> Sequence[CrossExecVariableDTO]: ...

    # -- Calibration registries ------------------------------------------

    def access_configs_list(self) -> Sequence[AccessConfigDTO]: ...

    def access_configs_get(self, name: str) -> AccessConfigDTO: ...

    def access_configs_add(self, config: AccessConfigDTO) -> AccessConfigDTO: ...

    def access_configs_update(
        self, config: AccessConfigDTO,
    ) -> AccessConfigDTO: ...

    def access_configs_delete(self, name: str) -> None: ...

    def grip_profiles_list(self) -> Sequence[GripProfileDTO]: ...

    def grip_profiles_get(self, labware_type: str) -> GripProfileDTO: ...

    def grip_profiles_patch(
        self, labware_type: str, body: GripProfilePatchRequest,
    ) -> GripProfileDTO: ...

    def grip_profiles_reset(self, labware_type: str) -> None: ...

    def move_defaults_list(self) -> Sequence[MoveDefaultsDTO]: ...

    def move_defaults_get(self, transporter_name: str) -> MoveDefaultsDTO: ...

    def move_defaults_patch(
        self, transporter_name: str, body: MoveDefaultsPatchRequest,
    ) -> MoveDefaultsDTO: ...

    def move_defaults_reset(self, transporter_name: str) -> None: ...

    def teachpoints_list(self, device_id: str) -> Sequence[TeachpointDTO]: ...

    def teachpoints_list_all(self) -> Sequence[TeachpointDTO]: ...

    def teachpoints_get(
        self, device_id: str, position_id: str,
    ) -> TeachpointDTO: ...

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
    ) -> TeachpointDTO: ...

    def teachpoints_set_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        body: GripProfilePatchRequest,
    ) -> TeachpointDTO: ...

    def teachpoints_clear_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
    ) -> None: ...

    def teachpoints_delete(self, device_id: str, position_id: str) -> None: ...

    def deck_layouts_list(
        self, device_id: str,
    ) -> Sequence[DeckLayoutSummaryDTO]: ...

    def deck_layouts_list_all(self) -> Sequence[DeckLayoutSummaryDTO]: ...

    def deck_layouts_get(self, device_id: str, name: str) -> DeckLayoutDTO: ...

    def deck_layouts_add(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> DeckLayoutDTO: ...

    def deck_layouts_update(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> DeckLayoutDTO: ...

    def deck_layouts_delete(self, device_id: str, name: str) -> None: ...

    # -- Reservations ----------------------------------------------------

    def list_reservations(
        self, execution_id: str,
    ) -> Sequence[ReservationSnapshotDTO]: ...

    def reservations_list_all(self) -> Sequence[ReservationSnapshotDTO]: ...

    def reservation_cancel(
        self, execution_id: str, reservation_id: str,
        reason: str | None = None,
    ) -> None: ...

    # -- Plugins (read-only) ---------------------------------------------

    def plugins_list(self) -> Sequence[PluginDTO]: ...

    def plugins_commands(self) -> Sequence[PluginCommandDTO]: ...

    # -- Labware catalog (operator CRUD; both backends via deployment registries) --

    def get_labware(self, labware_type: str) -> LabwareCatalogEntryDTO: ...

    def add_labware(
        self,
        *,
        labware_type: str,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntryDTO: ...

    def update_labware(
        self,
        labware_type: str,
        *,
        display_name: str,
        category: str,
        geometry: dict[str, JsonValue],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntryDTO: ...

    def delete_labware(self, labware_type: str) -> None: ...

    def list_labware(
        self, category: str | None = None,
    ) -> Sequence[LabwareCatalogSummaryDTO]: ...

    # -- Runtime device commands -----------------------------------------

    def device_capabilities(
        self, device_name: str,
    ) -> Sequence[CommandDescriptorDTO]: ...

    def device_execute(
        self, device_name: str, command: str,
        options: dict[str, JsonValue] | None = None,
        *, mode: WorkflowRunMode | None = None, confirm: bool = False,
    ) -> DeviceInvocationResultDTO: ...

    def device_invoke(
        self, device_name: str, capability: str,
        kwargs: dict[str, JsonValue] | None = None,
        *, mode: WorkflowRunMode | None = None, confirm: bool = False,
    ) -> DeviceInvocationResultDTO: ...

    def device_initialize(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> None: ...

    def device_connect(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> None: ...

    def device_disconnect(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> None: ...

    def device_reconcile_deck(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> ReconcileDeckResponse: ...

    def device_compare_deck(
        self, device_name: str, *, mode: WorkflowRunMode | None = None,
    ) -> CompareDeckResponse: ...

    def device_take_control(
        self, device_name: str, *, reason: str | None = None,
    ) -> None: ...

    def device_release_control(self, device_name: str) -> None: ...

    # -- Threads (per-execution reads + single-thread pause/resume/recover + spawn) --

    def list_threads(
        self, execution_id: str,
    ) -> Sequence[DaemonThreadSnapshotDTO]: ...

    def get_thread_detail(
        self, execution_id: str, thread_id: str,
    ) -> DaemonThreadSnapshotDTO: ...

    def pause_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None: ...

    def resume_thread(
        self, execution_id: str, thread_id: str, reason: str | None = None,
    ) -> None: ...

    def recover_thread(
        self, execution_id: str, thread_id: str, decision: str,
    ) -> None: ...

    def spawn_thread(
        self, execution_id: str, template_name: str,
        labware_id: str | None = None,
    ) -> DaemonThreadSnapshotDTO: ...

    # -- Labware instance ops --------------------------------------------

    def labware_list(self) -> Sequence[LabwareDTO]: ...

    def labware_get_by_id(self, labware_id: str) -> LabwareDTO: ...

    def labware_get_by_id_or_none(
        self, labware_id: str,
    ) -> LabwareDTO | None: ...

    def labware_get_by_barcode(self, barcode: str) -> LabwareDTO: ...

    def labware_get_by_barcode_or_none(
        self, barcode: str,
    ) -> LabwareDTO | None: ...

    def labware_history(
        self, labware_id: str,
    ) -> Sequence[LocationEventDTO]: ...

    def labware_edit_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None: ...

    def labware_edit_barcode(self, labware_id: str, barcode: str) -> None: ...

    def labware_set_carry_override(
        self, labware_id: str, patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (),
    ) -> MoveParameterPatch: ...

    def labware_clear_carry_override(self, labware_id: str) -> None: ...

    def labware_reset_location(
        self, labware_id: str, location: str, reason: str,
    ) -> None: ...

    def labware_register(
        self, template_name: str | None = None,
        labware_type: str | None = None,
        barcode: str | None = None,
        location: str | None = None,
    ) -> LabwareDTO: ...

    def labware_get_well_volumes(
        self, labware_id: str,
    ) -> GetWellVolumesResponse: ...

    def labware_set_well_volumes(
        self, labware_id: str, well_volumes: dict[str, float], reason: str,
    ) -> None: ...

    def labware_resolve_contents(
        self, labware_id: str,
    ) -> ResolveContentsResponse: ...

    def labware_mark_tips_used(
        self, labware_id: str, positions: list[str], reason: str,
    ) -> MarkTipsUsedResponse: ...

    def labware_get_tip_state(
        self, labware_id: str,
    ) -> GetTipStateResponse: ...

    def labware_set_tip_state(
        self, labware_id: str, tip_positions_present: list[str], reason: str,
    ) -> None: ...

    def labware_confirm_tip_state(
        self, labware_id: str, reason: str | None = None,
    ) -> None: ...

    def release_mover_hold(
        self, mover_name: str, reason: str,
        to_location: str | None = None, force: bool = False,
    ) -> MoverHoldReleaseDTO: ...

    def labware_confirm_well_volumes(
        self, labware_id: str, reason: str | None = None,
    ) -> None: ...

    # -- Submissions -----------------------------------------------------

    def submission_submit(
        self, request: SubmissionSubmitRequest,
    ) -> SubmitExecutionResponse: ...

    def submissions_list(
        self, execution_id: str | None = None,
    ) -> Sequence[SubmissionDTO]: ...

    def submission_get(self, submission_id: str) -> SubmissionDTO: ...

    # -- Catalog reads + system info -------------------------------------

    def list_thread_templates(self) -> Sequence[ThreadTemplateDTO]: ...

    def list_locations(self) -> Sequence[LocationDTO]: ...

    def system_info(self) -> SystemInfoDTO: ...

    def methods_list(self) -> Sequence[MethodSummaryDTO]: ...

    def method_get(
        self, name: str, workflow_name: str | None = None,
    ) -> MethodSummaryDTO: ...

    def workflow_get(self, name: str) -> WorkflowSummaryDTO: ...

    def topology_get(
        self, source: bool = False,
    ) -> TopologyViewDTO | TopologySourceDTO: ...

    def runtime_status(self) -> RuntimeStatusResponseDTO: ...

    # -- Recoverable-timeout operator decisions (both backends) ----------

    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float,
    ) -> RecoverableTimeoutDecisionResponse: ...

    def recoverable_timeout_abort(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse: ...

    def recoverable_timeout_mark_complete(
        self, incident_id: str, operator_name: str, reason: str,
    ) -> RecoverableTimeoutDecisionResponse: ...

    # -- Ops-history reads -----------------------------------------------

    def ops_history_get(self, execution_id: str) -> OpsHistoryGetResponseDTO: ...

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
    ) -> OpsHistorySearchResponseDTO: ...

    # -- Labware journey + runtime mutations -----------------------------

    def labware_journey(
        self, labware_id: str, kinds: Sequence[str] | None = None,
    ) -> LabwareJourneyResponseDTO: ...

    def labware_clear_submission(
        self, submission_id: str, force: bool = False,
    ) -> dict[str, list[str]]: ...

    def labware_discharge(
        self, labware_id: str, force: bool = False,
    ) -> dict[str, list[str]]: ...

    def labware_clear_all(
        self, force: bool = False,
    ) -> dict[str, list[str]]: ...

    def state_unsettled(self) -> UnsettledStateResponse: ...


@runtime_checkable
class ICloudControlPlaneClient(IControlPlaneClient, Protocol):
    """The extra surface a cloud backend serves on top of the shared one.

    A cloud deployment keeps code in a server-side worktree and runs a
    long-lived runtime, so it answers verbs a loopback daemon has no
    equivalent for: module and worktree reads, topology and workflow
    submission, runtime reload. Cloud-only CLI verbs are typed against this
    rather than a concrete class, which is what lets the source-available
    build ship with no cloud backend installed at all.
    """

    def __init__(self, *, base_url: str, api_key: str) -> None: ...

    @property
    def base_url(self) -> str: ...

    # -- Execution + thread verbs the loopback daemon does not serve ------

    def execution_close(self, execution_id: str) -> ExecutionCloseResponseDTO: ...

    def pause_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> None: ...

    def resume_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> ResumeAllResultDTO: ...

    def manual_steps_list(
        self, execution_id: str | None,
    ) -> list[PendingManualStepDTO]: ...

    def manual_step_confirm(self, execution_id: str, step_id: str) -> None: ...

    # -- Incidents -------------------------------------------------------

    def incidents_list(
        self,
        unacknowledged_only: bool = False,
        category: str | None = None,
        execution_id: str | None = None,
    ) -> list[IncidentDTO]: ...

    def incidents_get(self, incident_id: str) -> IncidentDTO: ...

    def incidents_ack(self, incident_id: str) -> IncidentAckResponse: ...

    def incidents_ack_all(
        self, category: str | None = None,
    ) -> IncidentAckResponse: ...

    # -- Code in the server-side worktree --------------------------------

    def modules_list(self) -> Sequence[GitFileEntryDTO]: ...

    def modules_get(self, kind: str, name: str) -> GitFileSourceDTO: ...

    def modules_delete(
        self, kind: str, name: str, message: str,
    ) -> SubmitModuleResponseDTO: ...

    def worktree_list(self) -> Sequence[WorktreeFileEntryDTO]: ...

    def worktree_get(self, path: str) -> WorktreeFileSourceDTO: ...

    # -- Source submission + reload --------------------------------------

    def list_workflows(self) -> Sequence[WorkflowSummaryDTO]: ...

    def workflow_load(
        self, name: str, source: str, message: str,
    ) -> SubmitModuleResponseDTO: ...

    def workflow_delete(
        self, name: str, message: str,
    ) -> SubmitModuleResponseDTO: ...

    def topology_submit(
        self, source: str, message: str,
    ) -> SubmitModuleResponseDTO: ...

    def topology_delete(
        self, message: str = "operator removal",
    ) -> SubmitModuleResponseDTO: ...

    def runtime_reload(self, reason: str = "") -> ReloadResponseDTO: ...

    def runtime_reload_workflow(self, reason: str = "") -> ReloadResponseDTO: ...
