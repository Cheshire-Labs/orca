"""ISystemRuntime: the single operational contract every UI programs against.

CLI (this repo), future REST, future MCP, future WebSocket — all consume the
same interface. Implementations:
  - SystemRuntime: the real one (this process).
  - FakeSystemRuntime: in-tests (future).
  - OrgScopedRuntime: a hosted wrapper (future, adds org_id enforcement).

The top-level `ISystemRuntime` is deliberately small (~20 methods). Power-user
operations (variable edits, labware moves, device sends, mutation) live on
namespaced sub-facade ABCs (variables, labware, devices, threads, registry,
incidents, submissions). This keeps the top surface reviewable, maps
naturally to CLI noun-verb grammar, and isolates each concern so a hosted
deployment can wrap just the parts it needs to org-scope.

All destructive methods take `confirm: bool = False` and raise
`ConfirmationRequired` when False — enforced by the `@dangerous` decorator
in the concrete facades. The UI calls `describe_action(name)` to get a
prompt template before confirming.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Callable, List, Literal, Optional, Protocol, Sequence, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, JsonValue

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, Teachpoint

from orca.gateway.device_fault import DeviceFault
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin, PluginCommand
from orca.state.records import TrackingRecord
from orca.runtime.danger import ActionDescriptor
from orca.runtime.execution import Execution, StopOutcome
from orca.runtime.execution_record import ExecutionRecord
from orca.runtime.incident_store import (
    IncidentCategory,
    SystemIncident,
)
from orca.runtime.interfaces import (
    IAccessConfigStore,
    IDeploymentProfileStore,
    IEventSink,
)
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.labware_catalog_service import LabwareCatalogService
from orca.state.ops_store import OpsHistorySearchQuery
from orca.runtime.labware_group import LabwareGroup
from orca.runtime.move_parameter_models import (
    LabwareGripProfile,
    TransporterMoveDefaults,
)
from orca.runtime.submission import BatchMode
from orca.system.resource_registry import IResourceRegistry
from orca.system.system_interface import DeckComparison, ISystem
from orca.state.mounted import MountedTips
from orca.state.unsettled import UnsettledSubject
from orca.state.contents import ContentsResolution
from orca.runtime.blockers import Blocker
from orca.runtime.status_models import (
    CommandDescriptor,
    ConnectionCard,
    DeviceIntrospection,
    DeviceFaultSummary,
    DeviceRegistryEntry,
    DeviceSnapshot,
    DeviceUnionEntry,
    ExecutionDetail,
    ExecutionStatus,
    GatewayDeviceEntry,
    LabwareSnapshot,
    LabwareTemplateSnapshot,
    LocationEvent,
    LocationSnapshot,
    MethodTemplateSnapshot,
    PendingManualStepRecord,
    PluginSnapshot,
    ReportedDeviceLink,
    ReservationSnapshot,
    ResourcePoolSnapshot,
    SubmissionCloseResult,
    SubmissionSnapshot,
    SystemInfoSnapshot,
    ThreadSnapshot,
    ThreadTemplateSnapshot,
    TipStateSnapshot,
    WellVolumesSnapshot,
    TopologyDeviceEntry,
    MoverSnapshot,
    TransporterSnapshot,
    WorkflowRunMode,
    WorkflowTemplateSnapshot,
)
from orca.variables.deployment_profile import DeploymentProfile
from orca.variables.errors import OptionValue
from orca.variables.resolution import VariableResolution
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.mutation_position import InsertPosition
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.workflow_templates import WorkflowTemplate


_P = TypeVar("_P", bound=OrcaPlugin)


# -- IThreadRuntimeAccess ----------------------------------------------------
# Narrow Protocol of the SystemRuntime methods ThreadFacade needs. Satisfied
# structurally by SystemRuntime, so ThreadFacade doesn't have to import the
# concrete class (avoiding an import cycle without resorting to TYPE_CHECKING).


class IThreadRuntimeAccess(Protocol):
    """Subset of ISystemRuntime methods ThreadFacade delegates to.

    Any object with these signatures can drive a ThreadFacade. SystemRuntime
    satisfies this structurally; tests can pass a FakeRuntime that provides
    only these methods. Existence of this Protocol avoids a circular import
    between threads.py and system_runtime.py.
    """

    def list_threads(self, execution_id: str) -> List[ThreadSnapshot]: ...
    def get_thread_detail(self, execution_id: str, thread_id: str) -> ThreadSnapshot: ...
    def get_paused_threads(self, execution_id: str) -> List[ThreadSnapshot]: ...

    def pause_thread(self, execution_id: str, thread_id: str) -> None: ...
    def resume_thread(self, execution_id: str, thread_id: str) -> None: ...
    def pause_all_threads(
        self, execution_id: str, reason: str = "manual", message: str | None = None,
    ) -> dict[str, int]: ...
    def resume_all_threads(self, execution_id: str) -> dict[str, int]: ...
    def pause_execution(
        self, execution_id: str, reason: str = "manual", message: str | None = None,
    ) -> dict[str, int]: ...
    def resume_execution(self, execution_id: str) -> dict[str, int]: ...
    def recover_thread(
        self, execution_id: str, thread_id: str, decision: RecoveryDecision,
    ) -> None: ...
    def fault_named_by_pause(
        self, execution_id: str, thread_id: str,
    ) -> DeviceFault | None: ...
    async def clear_fault_if_current(self, fault: DeviceFault) -> None: ...

    def mutate_on_next_pause(
        self, execution_id: str, thread_id: str,
        callback: Callable[[Any, str], None],
    ) -> None: ...

    async def spawn_thread_in_execution(
        self, execution_id: str, template_name: str,
        labware_id: str | None = None,
    ) -> ThreadSnapshot: ...


# -- IThreadMutationContext --------------------------------------------------
# Narrow surface passed to `mutate_on_next_pause` callbacks. Exposes only what
# mutation callbacks legitimately need, not the full ISystem god interface.


class IThreadMutationContext(Protocol):
    """Passed to `threads.mutate_on_next_pause` callbacks while a thread is PAUSED.

    Enables controlled, in-callback mutation without exposing internal registries.
    Callbacks run synchronously on the asyncio loop; keep them fast.
    """

    def set_variable(self, name: str, value: OptionValue, execution_id: str) -> None: ...
    def get_variable(self, name: str, execution_id: str) -> OptionValue: ...

    def skip_method(
        self, thread_id: str, *,
        method_id: str | None = None, method_name: str | None = None,
    ) -> None: ...

    def insert_method(
        self, thread_id: str, template: MethodTemplate,
        where: InsertPosition,
    ) -> None: ...

    def skip_action(
        self, thread_id: str, *,
        action_id: str | None = None, action_command: str | None = None,
    ) -> None: ...

    def insert_action(
        self, thread_id: str, template: ActionTemplate,
        where: InsertPosition,
    ) -> None: ...


# -- IVariableFacade ---------------------------------------------------------


class IVariableFacade(ABC):
    """Read/write variables scoped to an execution or globally.

    Reads are safe. Writes are `@dangerous` on the concrete implementation:
    they require `confirm=True` and emit confirmation prompts via the danger
    registry.

    Variables are resolved at action start; in-flight actions do NOT see
    edits made after they began. Operators should pause affected threads
    before editing to get a clean boundary.
    """

    @abstractmethod
    def get(self, name: str, execution_id: str) -> OptionValue:
        """Resolve for a submission that holds no override of its own.

        Submission partitions outrank this answer, so a caller that needs the
        value one submission's threads actually get uses :meth:`explain`.
        """

    @abstractmethod
    def explain(self, name: str, execution_id: str) -> VariableResolution:
        """Which value this execution resolves for ``name``, and from where.

        Lists each submission whose own partition outranks the execution-wide
        answer, so an operator can see what a mid-run edit will and will not
        reach.
        """

    @abstractmethod
    def get_global(self, name: str) -> OptionValue:
        """Resolve a global variable without an execution context.

        Walks the global value layer first, then global definition
        defaults. Computed variables are NOT evaluated here because
        their expressions may depend on execution-scoped state; callers
        that need computed resolution should query against a real
        execution via ``get(name, execution_id)``. ``name`` may be passed
        as either the bare identifier or with the ``global.`` prefix.
        """

    @abstractmethod
    def get_all(self, execution_id: str) -> dict[str, OptionValue]: ...

    @abstractmethod
    def has(self, name: str, execution_id: str) -> bool: ...

    @abstractmethod
    def has_global_value(self, name: str) -> bool:
        """True iff the global value layer holds an explicit value for ``name``.

        Distinct from :meth:`get_global` which falls through to global
        definition defaults; this returns False for a name that has a
        registered default but no explicit ``set_global`` call. The
        DELETE-on-globals envelope's ``existed`` field uses this so it
        accurately reports "the unset would actually pop a value" rather
        than "the name resolves through SOME layer."
        """

    @abstractmethod
    def has_execution_value(self, name: str, execution_id: str) -> bool:
        """True iff the per-execution partition holds a value for ``name``.

        Distinct from :meth:`has` which walks every layer (submission,
        execution partition, global, computed, definition default).
        Returns False for an unknown ``execution_id`` rather than
        raising. The DELETE-on-per-execution envelope's ``existed``
        field uses this so it accurately reports partition-membership
        rather than resolvability.
        """

    @abstractmethod
    def has_submission_value(
        self, name: str, execution_id: str, submission_id: str,
    ) -> bool:
        """True iff this submission's own partition holds ``name``.

        Partition membership, not resolvability: a name that resolves through
        the execution, global, or default layers returns False here because
        clearing the submission override would pop nothing.
        """

    @abstractmethod
    def set(
        self, name: str, value: OptionValue, execution_id: str, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    def set_global(
        self, name: str, value: OptionValue, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    def set_submission(
        self, name: str, value: OptionValue, execution_id: str,
        submission_id: str, *, confirm: bool = False,
    ) -> None:
        """Write the layer that outranks the per-execution partition.

        Values supplied on a submit land here, so this is the only write that
        changes what an already-submitted thread resolves.
        """

    @abstractmethod
    def unset(
        self, name: str, execution_id: str, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    def unset_submission(
        self, name: str, execution_id: str, submission_id: str, *,
        confirm: bool = False,
    ) -> None:
        """Drop one submission's override so it falls through to the layers below."""

    @abstractmethod
    def unset_global(
        self, name: str, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    def load_profile(
        self, execution_id: str, profile_path: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...


# -- ILabwareFacade ----------------------------------------------------------


class MoverHoldRelease(BaseModel):
    """What a mover-hold release did.

    ``released_to`` names where the operator said the labware is; it is None
    exactly when ``discharged`` is True.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mover_name: str
    labware_id: str
    labware_name: str
    released_to: str | None
    discharged: bool


class ClearSubmissionResult(BaseModel):
    """Return shape for `ILabwareFacade.clear_submission_labware`.

    Surfaces the SKIPPED reuse-bound labware so the caller can see what
    survived the clear -- a flat `list[cleared_ids]` would silently drop
    that information. Used end-to-end: facade -> a hosted deployment's REST/MCP/CLI ->
    cloud client; serialized verbatim into the response envelope.
    """

    cleared: list[str]
    preserved_reuse_bound: list[str]


class ILabwareFacade(ABC):
    """Labware identity and location.

    Wraps both `ILabwareLocationService` (location tracking) and
    `ILabwareStore` (identity by id/barcode). All methods are async for
    uniformity with the async store and future DB-backed implementations.

    `edit_location` is the operator override for manual moves; no
    transporter is invoked. It refuses only a target nothing could be put down
    at: one already holding different labware, or one another thread has
    reserved. A thread using the labware is never a refusal; it is told to plan
    its move again from the position the operator gave.
    """

    @abstractmethod
    async def get_by_id(self, labware_id: str) -> LabwareSnapshot: ...

    @abstractmethod
    async def get_by_barcode(self, barcode: str) -> LabwareSnapshot: ...

    @abstractmethod
    async def list_all(self) -> list[LabwareSnapshot]: ...

    @abstractmethod
    async def get_history(self, labware_id: str) -> list[LocationEvent]: ...

    @abstractmethod
    async def edit_location(
        self, labware_id: str, location: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def edit_barcode(
        self, labware_id: str, new_barcode: str, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def set_carry_override(
        self, labware_id: str,
        patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (),
        *, confirm: bool = False,
    ) -> MoveParameterPatch: ...

    @abstractmethod
    async def clear_carry_override(
        self, labware_id: str, *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def reset_location(
        self, labware_id: str, location: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def register(
        self, template_name: str | None = None, *,
        labware_type: str | None = None,
        barcode: str | None = None,
        location: str | None = None,
        confirm: bool = False,
    ) -> LabwareSnapshot:
        """Record labware an operator has put down. Exactly one of
        ``template_name`` (declared in the deployment package) or
        ``labware_type`` (a catalog definition nothing declared, which derives
        an ad-hoc template)."""
        ...

    @abstractmethod
    async def get_well_volumes(self, labware_id: str) -> WellVolumesSnapshot:
        """Per-well current volume folded from the labware's ledger ops, with
        how well the record knows it.

        Empty when the labware has no seeded or operator-set wells, which
        ``provenance`` tells apart from known-to-be-empty. Read-only; no
        confirmation required.
        """
        ...

    @abstractmethod
    async def set_well_volumes(
        self, labware_id: str, volumes: dict[str, float], *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Operator-set absolute per-well volumes (PHYSICAL, requires reason).

        Writes a SET_VOLUME ledger record (source=OPERATOR, WHAT only), seeds
        the driver tracker immediately if the labware is on-deck, and otherwise
        lets the next placement seed it ledger-first. Rejects volumes above a
        well's known capacity.
        """
        ...

    @abstractmethod
    async def mark_tips_used(
        self, labware_id: str, positions: list[str], *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> list[str]:
        """Operator-asserted subtraction (PHYSICAL, requires reason).

        Records that these positions no longer hold a tip and returns what the
        rack still holds. Every other position keeps its tracked state, so the
        ordinary repair -- a pick found air, a hand took a column -- does not
        mean restating the whole rack."""
        ...

    @abstractmethod
    async def resolve_contents(self, labware_id: str) -> ContentsResolution:
        """What this labware holds: THE answer, plus every layer's reading.

        The canonical contents read. ``get_tip_state`` and a driver's own deck
        state are the raw layers feeding it, kept for diagnosis; anything that
        just needs the number asks here and stops comparing endpoints."""
        ...

    @abstractmethod
    async def get_tip_state(self, labware_id: str) -> TipStateSnapshot:
        """The rack's folded tip layout on its own, without the other layers.

        A raw view: prefer ``resolve_contents`` unless you specifically want the
        ledger's reading in isolation."""
        ...

    @abstractmethod
    async def set_tip_state(
        self, labware_id: str, tip_positions_present: list[str], *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Operator-set absolute tip layout (PHYSICAL, requires reason).

        Writes a SET_TIP_STATE ledger record (source=OPERATOR): tips at the
        named positions, nowhere else. Seeds the driver rack immediately if
        on-deck and clears the rack's stale mark.
        """
        ...

    @abstractmethod
    async def confirm_well_volumes(
        self, labware_id: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Operator agreement that the tracked well volumes match the labware.

        The volume half of ``confirm_tip_state``. Refused when the record holds
        no volumes to agree with.
        """
        ...

    @abstractmethod
    async def confirm_tip_state(
        self, labware_id: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Operator agreement that the tracked tip layout matches the rack.

        Appends the current projection as a SET_TIP_STATE baseline and clears
        the stale mark. Refused when nothing is tracked to agree with.
        """
        ...

    @abstractmethod
    async def clear_submission_labware(
        self, submission_id: str, *, force: bool = False,
    ) -> ClearSubmissionResult:
        """Remove non-reuse-bound labware associated with a submission.

        Walks every thread tied to ``submission_id``; for each one whose
        thread template does NOT declare ``end_leave_in_place`` (or
        equivalently is not a reuse-bound persistent labware path),
        clears the labware from its current Location, ``system.labwares``,
        and ``_labware_store``. Reuse-bound labware is SKIPPED so deck-
        resident reagents survive the clear.

        Refuses when the submission's execution is non-terminal unless
        ``force=True``.

        Returns both the cleared `labware_id` list and the SKIPPED
        reuse-bound list so the caller surfaces what survived (a flat
        list would silently drop the reuse-bound information).
        """
        ...

    @abstractmethod
    async def release_mover_hold(
        self, mover_name: str, to_location: str | None = None, *,
        force: bool = False,
        reason: str | None = None,
        confirm: bool = False,
    ) -> MoverHoldRelease:
        """Free one mover the record says is holding a labware.

        An abort taken while a plate is genuinely in the jaws leaves a real
        hold, and a mover holding one refuses every later pick. Addressed by
        the mover because the mover is what the refusal names.

        ``to_location`` states where the labware really is and asserts it
        there, which frees the jaws and tells the thread carrying it to plan a
        fresh move. Omitting it discharges the labware, for when the jaws are
        empty and the record is wrong; that path refuses while a live thread
        still carries the labware unless ``force``.
        """
        ...

    @abstractmethod
    async def discharge_labware(
        self, labware_id: str, *, force: bool = False,
    ) -> None:
        """Remove ONE labware instance from the runtime state.

        Operator-initiated: "I picked it up physically." Clears the
        instance from ``system.labwares``, the labware store, and any
        Location that currently holds it. Refuses if any active
        execution has a thread referencing this instance unless
        ``force=True``. The one exemption is the thread that asked for
        the removal: a thread parked at ``AWAITING_MANUAL_REMOVE`` on
        this instance never triggers the refusal.
        """
        ...

    @abstractmethod
    async def clear_all_labware(
        self, *, force: bool = False,
    ) -> list[str]:
        """Panic button: clear every Location and remove every labware.

        Refuses if any active execution exists unless ``force=True``.
        Returns the list of cleared labware_ids.
        """
        ...


# -- IDeviceFacade -----------------------------------------------------------


class IDeviceFacade(ABC):
    """Direct device command invocation outside of workflow actions.

    `execute` calls `IGenericExecutable.execute(command, options)` on the
    device driver. `invoke` dispatches to a specific capability method
    (e.g. `shaker.shake`) discovered via `get_supported_commands`.

    Commands that would contend with an in-progress action refuse by default;
    `--force` (confirm=True in the concrete impl) bypasses the check with an
    audible warning.

    The lifecycle is four separable steps: `connect` opens the link,
    `initialize` makes the device ready to command, the driver's own `home`
    moves it (reached through `invoke`, since homing has no verb of its own
    here), and `disconnect` hands it back. They stay separate on purpose, so
    that bringing a device up is never a request to move it: an arm that has
    not homed since power-on refuses the first move that needs a known
    position, and that refusal is the prompt to home deliberately. The
    exception is a driver whose vendor library offers only a compound bring-up,
    which homes inside `initialize` because it has no other way in. Of the
    three verbs this facade owns, only `connect` is free of consequence, so it
    alone is undecorated.

    `list_devices` returns the union view across `runtime.topology` and
    `runtime.gateway`: each entry carries `in_topology`, `gateway_connected`,
    `last_heartbeat`, and the topology / observed driver classes. Topology
    is authoritative for what a device should be; the gateway is
    authoritative for whether it is reachable right now.
    """

    @abstractmethod
    async def list_devices(self) -> list[DeviceUnionEntry]: ...

    @abstractmethod
    def get_device_status(self, device_name: str) -> DeviceSnapshot: ...

    @abstractmethod
    def get_supported_commands(self, device_name: str) -> list[CommandDescriptor]: ...

    @abstractmethod
    def get_device_introspection(self, device_name: str) -> DeviceIntrospection: ...

    @abstractmethod
    async def execute(
        self, device_name: str, command: str,
        options: dict[str, JsonValue] | None = None, *,
        mode: WorkflowRunMode | None = None,
        confirm: bool = False,
        vendor_confirm: bool = False,
    ) -> "DeviceInvocationResult": ...

    @abstractmethod
    async def invoke(
        self, device_name: str, capability: str, kwargs: dict[str, JsonValue], *,
        mode: WorkflowRunMode | None = None,
        confirm: bool = False,
        vendor_confirm: bool = False,
    ) -> "DeviceInvocationResult": ...

    @abstractmethod
    async def initialize(
        self, device_name: str, *,
        mode: WorkflowRunMode | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def connect(
        self, device_name: str, *,
        mode: WorkflowRunMode | None = None,
    ) -> None: ...

    @abstractmethod
    async def disconnect(
        self, device_name: str, *,
        mode: WorkflowRunMode | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def reconcile_deck(
        self, device_name: str, *,
        mode: WorkflowRunMode | None = None,
        confirm: bool = False,
    ) -> DeckComparison | None:
        """Re-seed one liquid handler from the world model (layout + occupancy).

        A state push, no motion. The operator's lever after a driver session
        was rebuilt outside orca; lifecycle verbs dispatched through orca
        re-seed automatically. Refuses non-liquid-handler devices.

        Returns what the driver said BEFORE the push overwrote it, because
        afterwards there is nothing left to compare. None when the device has
        no deck layout configured.
        """
        ...

    @abstractmethod
    async def take_external_control(
        self, device_name: str, reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Claim a device for hands-on work until it is released.

        While held, workflow dispatch on the device, and moves into or out of
        it, raise ``DeviceUnderExternalControlError``.
        """
        ...

    @abstractmethod
    async def release_external_control(
        self, device_name: str, confirm: bool = False,
    ) -> None:
        """Hand the device back to the workflow. Idempotent."""
        ...

    @abstractmethod
    async def clear_fault(
        self, device_name: str, confirm: bool = False,
    ) -> DeviceFaultSummary | None:
        """Say a faulted device has been looked at. Returns what was cleared.

        A fault stands from the moment a command left the device part-way
        through something until a person says otherwise, because only a person
        can say the instrument is fit to drive. A clean `initialize` or `home`
        says it too: they are what an operator runs to put a machine into a
        known state, and the engine cannot run either on a faulted device.
        """
        ...

    @abstractmethod
    async def get_mounted_tips(self, device_name: str) -> MountedTips:
        """What the record says this device's head is carrying."""
        ...

    @abstractmethod
    async def set_mounted_tips(
        self, device_name: str, by_channel: dict[int, tuple[str, str]],
        *, reason: str | None = None, confirm: bool = False,
    ) -> None:
        """State what the head is carrying. Absolute: a channel left out is
        recorded as carrying nothing."""
        ...

    @abstractmethod
    async def confirm_mounted_tips(
        self, device_name: str, *, reason: str | None = None, confirm: bool = False,
    ) -> None:
        """Agree with what the record already says the head is carrying."""
        ...

    @abstractmethod
    async def compare_deck(
        self, device_name: str, *,
        mode: WorkflowRunMode | None = None,
    ) -> DeckComparison:
        """What this liquid handler's deck holds, against the ledger.

        Reads only and changes neither side. Every difference is filed as an
        incident as well. Refuses non-liquid-handler devices and ones with no
        deck layout configured.
        """
        ...


# -- IThreadFacade -----------------------------------------------------------


class IThreadFacade(ABC):
    """Per-thread control: inspection, pause/resume/recover, and mutation.

    `recover` applies a `RecoveryDecision` (RETRY / RETRY_OP / CONTINUE /
    ABORT_ACTION / ABORT_METHOD / ABORT_THREAD) to an error-paused thread.
    RETRY_OP is valid only while a device op is paused (re-runs that call,
    action body stays suspended). CONTINUE is valid only while an action is
    error-paused: it advances to the next action, which needs a bound action
    and labware whose position is known. Manually paused threads must use
    `resume` instead; the concrete impl enforces the distinction with a clear
    error message.

    Mutation operations (`skip_method`, `abort_method`, `insert_method`,
    `skip_action`, `insert_action`) require the target thread to be PAUSED.
    Insert operations take an `InsertPosition` descriptor (AtHead / AtTail /
    Before / After). `skip_method` targets pending methods; `abort_method`
    targets the currently-assigned IN_PROGRESS method (the only one that has
    started and so cannot simply be skipped).

    All four pause/resume operations (``pause`` / ``resume`` /
    ``pause_all`` / ``resume_all``) are decorated with ``@dangerous`` and
    require ``confirm=True``. ``resume`` and ``resume_all`` joined this
    contract in the 2026-05-07 walk so the audit trail covers cancel-pause
    cases too; pre-existing programmatic callers must pass ``confirm=True``
    or they will raise ``ConfirmationRequired``. ``reason`` is optional;
    when provided it lands on the audit record.
    """

    @abstractmethod
    def list(self, execution_id: str) -> List[ThreadSnapshot]: ...

    @abstractmethod
    def get(self, execution_id: str, thread_id: str) -> ThreadSnapshot: ...

    @abstractmethod
    def list_paused(self, execution_id: str) -> List[ThreadSnapshot]: ...

    @abstractmethod
    async def pause(
        self, execution_id: str, thread_id: str,
        reason: str | None = None, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def resume(
        self, execution_id: str, thread_id: str,
        reason: str | None = None, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def pause_all(
        self, execution_id: str,
        reason: str | None = None, *,
        confirm: bool = False,
    ) -> dict[str, int]: ...

    @abstractmethod
    async def resume_all(
        self, execution_id: str,
        reason: str | None = None, *,
        confirm: bool = False,
    ) -> dict[str, int]: ...

    @abstractmethod
    async def recover(
        self, execution_id: str, thread_id: str, decision: RecoveryDecision, *,
        confirm: bool = False,
    ) -> None: ...

    # Mutation ops (require PAUSED thread; enforced by MutationCoordinator)

    @abstractmethod
    async def skip_method(
        self, execution_id: str, thread_id: str, *,
        method_id: str | None = None,
        method_name: str | None = None,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...

    @abstractmethod
    async def abort_method(
        self, execution_id: str, thread_id: str, *,
        method_id: str | None = None,
        method_name: str | None = None,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...

    @abstractmethod
    async def insert_method(
        self, execution_id: str, thread_id: str,
        template: MethodTemplate,
        where: InsertPosition, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...

    @abstractmethod
    async def skip_action(
        self, execution_id: str, thread_id: str, *,
        action_id: str | None = None,
        action_command: str | None = None,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...

    @abstractmethod
    async def insert_action(
        self, execution_id: str, thread_id: str,
        template: ActionTemplate,
        where: InsertPosition, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...

    @abstractmethod
    async def replace_method(
        self, execution_id: str, thread_id: str,
        target_name: str, template: MethodTemplate, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> bool:
        """Replace a method. Returns True if staged for recovery (errored
        target -> drop via recover_thread(ABORT_METHOD)), False if fully
        spliced. `reason` is consumed by the @dangerous audit trail."""
        ...

    @abstractmethod
    async def replace_action(
        self, execution_id: str, thread_id: str,
        target_command: str, template: ActionTemplate, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> bool:
        """Replace an action. Returns True if staged for recovery (errored
        target -> drop via recover_thread(ABORT_ACTION)), False if fully
        spliced. `reason` is consumed by the @dangerous audit trail."""
        ...

    @abstractmethod
    async def mutate_on_next_pause(
        self, execution_id: str, thread_id: str,
        callback: Callable[[IThreadMutationContext, str], None], *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def spawn_thread(
        self, execution_id: str, template_name: str, *,
        labware_id: str | None = None,
        confirm: bool = False,
    ) -> ThreadSnapshot: ...


# -- IRegistryFacade ---------------------------------------------------------


class IRegistryFacade(ABC):
    """Read-only inventory (mostly) plus targeted mutations that live here for
    organizational reasons.

    Topology mutations (add/remove devices, locations, resource pools) are
    not exposed: doing so safely at runtime requires live device-config
    instantiation + reservation-aware removal, which is its own design
    effort. Reservation cancellation is exposed because it operates on the
    existing topology.
    """

    @abstractmethod
    def system_info(self) -> SystemInfoSnapshot: ...

    @abstractmethod
    def list_devices(self) -> list[DeviceSnapshot]: ...

    @abstractmethod
    def list_transporters(self) -> list[TransporterSnapshot]: ...

    @abstractmethod
    def list_movers(self) -> list[MoverSnapshot]: ...

    @abstractmethod
    def list_resource_pools(self) -> list[ResourcePoolSnapshot]: ...

    @abstractmethod
    def list_locations(self) -> list[LocationSnapshot]: ...

    @abstractmethod
    def list_labware_templates(self) -> list[LabwareTemplateSnapshot]: ...

    @abstractmethod
    def list_method_templates(self) -> list[MethodTemplateSnapshot]: ...

    @abstractmethod
    def get_method_template(self, workflow_name: str, name: str) -> MethodTemplate:
        """Return the runtime MethodTemplate registered under `(workflow_name, name)`.

        Used by wire callers (REST/MCP `thread_insert_method` with
        `template_name`) to resolve a name into a callable template that
        can be passed to `runtime.threads.insert_method`. Method names are
        unique per workflow, so the workflow scope is required. The
        InsertMethod operation derives `workflow_name` from the target
        thread's execution. Raises KeyError if no template exists.

        Action templates have no analogous getter: ActionTemplates are
        not registered system-wide. Wire callers must inject action
        source via `action_code` instead of name lookup.
        """
        ...

    @abstractmethod
    def list_workflow_templates(self) -> list[WorkflowTemplateSnapshot]: ...

    @abstractmethod
    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        """Return the runtime WorkflowTemplate registered under `name`.

        Used by wire callers (REST/MCP `method_execute_standalone`,
        execution submit) to resolve a name into a callable template.
        Raises KeyError if no template exists by that name.
        """
        ...

    @abstractmethod
    async def add_workflow_template(
        self, template: WorkflowTemplate, *,
        source_sha: str | None = None,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Register a workflow template (with its bundled methods + threads).

        Always REPLACE semantics: if a workflow with this name already
        exists, drop the old (cascade its bundled methods + threads) and
        register the new. Source-of-truth is git; rollback is via git
        revert + re-register.

        @dangerous-decorated. `reason` flows to the audit trail; the
        body ignores it. `source_sha` annotates the template with the
        git SHA it came from for the runtime/reload reconciliation pass.
        """
        ...

    @abstractmethod
    async def remove_workflow_template(
        self, name: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Drop the named workflow template + cascade its bundled methods/threads.

        Raises ``ValueError`` when an active execution is running this
        template; caller (REST handler) translates to 409. @dangerous.
        """
        ...

    @abstractmethod
    def list_thread_templates(self) -> list[ThreadTemplateSnapshot]: ...

    @abstractmethod
    def list_reservations(self, execution_id: str) -> list[ReservationSnapshot]: ...

    @abstractmethod
    async def cancel_reservation(
        self, execution_id: str, reservation_id: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...


# -- IIncidentFacade ---------------------------------------------------------


class IIncidentFacade(ABC):
    """Non-Action error records. See `incident_store.py` for category list.

    Incidents surface in real time through the event stream with
    `entity_type="INCIDENT"` so live UIs (watch / follow) pick them up.
    """

    @abstractmethod
    async def list(
        self, *,
        unacknowledged_only: bool = False,
        category: IncidentCategory | None = None,
        execution_id: str | None = None,
        since: float | None = None,
    ) -> List[SystemIncident]: ...

    @abstractmethod
    async def get(self, incident_id: str) -> SystemIncident: ...

    @abstractmethod
    async def acknowledge(
        self, incident_id: str, *,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def acknowledge_all(
        self, *,
        category: IncidentCategory | None = None,
        confirm: bool = False,
    ) -> int: ...


# -- IAccessConfigFacade -----------------------------------------------------


class IAccessConfigFacade(ABC):
    """Operator-facing CRUD for named approach/retract access patterns.

    Access configs are the global registry of named patterns that
    teachpoints reference. Mutation surface used by REST/MCP; the runtime
    itself reads through the underlying `IAccessConfigStore` directly.

    Reads pass through. Writes raise `ProtectedAccessConfigError` /
    `AccessConfigInUseError` from `orca.runtime.access_config_store` when
    the deployment-defined safety contract bites.
    """

    @abstractmethod
    async def get(self, name: str) -> AccessConfig | None: ...

    @abstractmethod
    async def list(self) -> List[AccessConfig]: ...

    @abstractmethod
    async def add(
        self, config: AccessConfig, *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, config: AccessConfig, *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(
        self, name: str, *, confirm: bool = False,
    ) -> bool: ...


# -- IMoveDefaultsFacade -----------------------------------------------------


class IMoveDefaultsFacade(ABC):
    """Operator-facing read/edit for what an arm's moves start from.

    One record per transporter, sparse underneath: an edit names the fields it
    changes and leaves the rest inheriting. Every record says which layer decided
    each field, so a number this deployment set is distinguishable from one that
    is simply the built-in seed.

    Reads never create a row. A transporter nobody has tuned reads as pure seed
    and keeps reading that way until somebody edits it.
    """

    @abstractmethod
    async def get(self, transporter_name: str) -> TransporterMoveDefaults: ...

    @abstractmethod
    async def list(
        self, system: IResourceRegistry | None = None,
    ) -> List[TransporterMoveDefaults]: ...

    @abstractmethod
    async def apply(
        self,
        transporter_name: str,
        patch: MoveParameterPatch,
        clear: Iterable[MoveParameterField] = (),
        *,
        confirm: bool = False,
    ) -> TransporterMoveDefaults: ...

    @abstractmethod
    async def reset(
        self, transporter_name: str, *, confirm: bool = False,
    ) -> bool: ...


# -- IGripProfileFacade ------------------------------------------------------


class IGripProfileFacade(ABC):
    """Operator-facing read/edit for how each labware type is held.

    One record per labware type, sparse: an edit names the fields it changes and
    leaves the rest to the layers underneath. A profile is reported as the patch
    it is rather than as resolved numbers, because it belongs to the type and not
    to any one arm.

    Reads never create a row. A type nobody has measured reads as an empty
    profile and keeps reading that way until somebody edits it.
    """

    @abstractmethod
    async def get(self, labware_type: str) -> LabwareGripProfile: ...

    @abstractmethod
    async def list(self) -> List[LabwareGripProfile]: ...

    @abstractmethod
    async def apply(
        self,
        labware_type: str,
        patch: MoveParameterPatch,
        clear: Iterable[MoveParameterField] = (),
        *,
        confirm: bool = False,
    ) -> LabwareGripProfile: ...

    @abstractmethod
    async def reset(
        self, labware_type: str, *, confirm: bool = False,
    ) -> bool: ...


# -- IDeckLayoutFacade -------------------------------------------------------


class IDeckLayoutFacade(ABC):
    """Operator-facing CRUD for deck layouts scoped per-liquid-handler.

    Each method takes a `device_id` because deck-layout namespaces are
    per-device. Resolution looks up the liquid handler on the System
    graph; an unknown device or a resource that is not a liquid handler
    raises `KeyError`.

    Edits are rebuild-required from the runtime's perspective: a
    running `LiquidHandler` is configured at start and cannot safely
    reconfigure mid-run because physical labware can't reposition while
    motion is in flight. CRUD writes the registry; the next
    `RuntimeLifecycle.rebuild()` picks up the new layout. REST/MCP
    response bodies should communicate this contract.

    Reads pass through. Writes inherit whatever safety contract the
    underlying store implements; the facade does not add a second
    layer of validation.
    """

    @abstractmethod
    async def get(
        self, device_id: str, name: str,
    ) -> DeckLayoutConfig | None: ...

    @abstractmethod
    async def list(
        self, device_id: str,
    ) -> List[tuple[str, DeckLayoutConfig]]: ...

    @abstractmethod
    async def list_all(
        self,
    ) -> List[tuple[str, str, DeckLayoutConfig]]: ...

    @abstractmethod
    async def add(
        self, device_id: str, name: str, config: DeckLayoutConfig,
        *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, device_id: str, name: str, config: DeckLayoutConfig,
        *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(
        self, device_id: str, name: str,
        *, confirm: bool = False,
    ) -> bool: ...


# -- ITeachpointFacade -------------------------------------------------------


class ITeachpointFacade(ABC):
    """Operator-facing CRUD for teachpoints scoped per-transporter.

    Each method takes a `device_id` because teachpoints are per-transporter
    (one Position can be reached by N transporters, each with its own
    Teachpoint to that Position). The store keys on `position_id` only;
    the cross-transporter 2-tuple `(device_id, position_id)` lives at this
    facade. Resolution looks up the transporter on the System graph; an
    unknown device raises `KeyError`.

    Reads pass through the per-device `ITeachpointStore`. Writes inherit
    whatever safety contract the underlying store implements: the source-available
    InMemory store is permissive; a hosted deployment's DB-backed store enforces uniqueness
    via `(device_id, position_id)`. The facade does not add a second layer
    of validation; the store is the source of truth.
    """

    @abstractmethod
    async def get(
        self, device_id: str, position_id: str,
    ) -> Teachpoint | None: ...

    @abstractmethod
    async def list(self, device_id: str) -> List[Teachpoint]: ...

    @abstractmethod
    async def list_all(self) -> List[tuple[str, Teachpoint]]: ...

    @abstractmethod
    async def add(
        self, device_id: str, teachpoint: Teachpoint,
        *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, device_id: str, teachpoint: Teachpoint,
        *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(
        self, device_id: str, position_id: str,
        *, confirm: bool = False,
    ) -> bool: ...

    @abstractmethod
    async def set_taught_with(
        self, device_id: str, position_id: str, labware_type: str | None,
        *, confirm: bool = False,
    ) -> Teachpoint: ...

    @abstractmethod
    async def apply_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        patch: MoveParameterPatch,
        clear: Sequence[str] = (),
        *, confirm: bool = False,
    ) -> Teachpoint: ...

    @abstractmethod
    async def clear_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        *, confirm: bool = False,
    ) -> bool: ...


# -- ISubmissionFacade -------------------------------------------------------


class ISubmissionFacade(ABC):
    """T6 LabwareGroup/Submission API: multi-group batch submission.

    Each call to `submit_group` accepts a workflow name plus one or more
    LabwareGroups. The SystemRuntime decides whether to inject the groups
    into a live ACCEPTING execution or boot a fresh one (STANDALONE always
    tolerates booting new; JOIN_EXISTING requires an ACCEPTING target and
    is rejected if the matching execution is DRAINING).

    `close_execution` transitions an execution from ACCEPTING to DRAINING
    so JOIN_EXISTING submissions are rejected; STANDALONE submissions for
    the same workflow start a fresh execution. Live threads in the draining
    execution continue to completion.
    """

    @abstractmethod
    async def submit_group(
        self, *,
        workflow_name: str,
        groups: tuple[LabwareGroup, ...] = (),
        variables: dict[str, OptionValue] | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        operator_id: str | None = None,
        deployment_profile: str | None = None,
        mode: WorkflowRunMode | None = None,
        acknowledge_warnings: bool = False,
        confirm: bool = False,
    ) -> SubmissionSnapshot: ...

    @abstractmethod
    def list_submissions(
        self, *, execution_id: str | None = None,
    ) -> list[SubmissionSnapshot]: ...

    @abstractmethod
    def get_submission(self, submission_id: str) -> SubmissionSnapshot: ...

    @abstractmethod
    def close_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> SubmissionCloseResult: ...


# -- IDeploymentProfileFacade ------------------------------------------------


class IDeploymentProfileFacade(ABC):
    """CRUD over named DeploymentProfile bundles.

    A profile is a named bag of variable defaults. At submit time, the
    runtime resolves the profile by name (`runtime.profile_store.get(...)`) and
    hands it to ``variable_store.load_profile``, which copies values into
    the execution partition + global layer + computed dict in one shot.
    The variable store is the only resolver after that.

    Reads are safe. Writes are `@dangerous`. Editing a profile via this
    facade does NOT affect any execution that is already running; the new
    body is applied to executions submitted AFTER the edit.
    """

    @abstractmethod
    async def get(self, name: str) -> DeploymentProfile | None: ...

    @abstractmethod
    async def list(self) -> list[DeploymentProfile]: ...

    @abstractmethod
    async def add(
        self, profile: DeploymentProfile, *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, profile: DeploymentProfile, *, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(self, name: str, *, confirm: bool = False) -> bool: ...


# -- ITopologyRegistry / IGatewayRegistry -----------------------------------


class ITopologyRegistry(Protocol):
    """Read-only view of devices declared in the deployment topology.

    Backed by the System graph: each `Transporter(...)`, `Shaker(...)`,
    `LiquidHandler(...)` etc. registers a Resource on
    `system.resource_registry`. The source-available implementation
    (`SystemTopologyRegistry`) walks that registry on demand. This Protocol
    is what `runtime.topology` exposes; consumers (DeviceFacade, the
    collision validator, a hosted REST/MCP surface) program against it instead of
    reaching into the System directly.
    """

    def list_devices(self) -> list[TopologyDeviceEntry]: ...
    def get_device(self, name: str) -> TopologyDeviceEntry | None: ...


class IGatewayRegistry(Protocol):
    """Read-only view of devices reachable via the device-integration gateway.

    a hosted deployment provides `DbGatewayRegistry`, fed by the orca-client WebSocket
    handshake plus the `RegisteredDevice` Postgres row. orca-core ships
    `NullGatewayRegistry` (always empty) so a local run with no gateway
    works out of the box. Mutations (force-disconnect, delete) are out of
    scope today; this is a read surface only.
    """

    async def list_connected(self) -> list[GatewayDeviceEntry]: ...
    async def get_gateway_status(self, name: str) -> GatewayDeviceEntry | None: ...


class IDeviceFaultSource(Protocol):
    """Which devices were left part-way through a command nobody has checked.

    The device gateway latches a fault when a command fails, times out, or is
    cancelled after it went out on the wire, and keeps it until an operator
    clears it. Read so device status can report it; cleared so the workflow can
    drive the device again.
    """

    def fault(self, device_id: str) -> DeviceFault | None: ...
    async def clear_fault(self, device_id: str) -> DeviceFault | None: ...


class TopologyCollisionError(RuntimeError):
    """Raised when a connected gateway's interfaces do not satisfy the topology contract.

    The safety contract is interface superset: a connected device's
    advertised interfaces MUST be a superset of the topology-declared
    interfaces (the methods the workflow will dispatch). Missing
    interfaces would crash the workflow at the wire. ``DeviceRegistryImpl
    ._verify_kind_match`` raises this error so the runtime fails loud
    rather than dispatching against a contract the wire cannot honor.

    Kind drift (different ``kind`` strings on either side) is advisory:
    multiple kind labels may satisfy the same interface contract. The
    startup collision validator logs a warning for kind drift and only
    raises this error on interface contract breaks.
    """


# -- Submit-time run-mode validation ----------------------------------------


class RunModeRequiredError(ValueError):
    """Raised when `SystemRuntime.submit` is called without a `mode=` value.

    Sim-hierarchy v3.4 makes `Submission.run_mode` required. The operator
    must declare PURE_SIM / DEVICE_SIM / LIVE at submit time; there is no
    deployment-level fallback.
    """

    def __init__(self) -> None:
        super().__init__(
            "Submission requires a `run_mode` (PURE_SIM, DEVICE_SIM, or LIVE). "
            "No deployment-level default is applied at submit time."
        )


class SubmissionToPausedExecutionError(RuntimeError):
    """Raised when a JOIN_EXISTING submission targets a paused execution.

    A paused execution is still ACCEPTING but its pause latch gates new work,
    so a join is refused rather than queued (queuing would hide the paused
    state from the operator). Resume the execution to accept joins, or submit
    STANDALONE to start a fresh execution. Engine-state refusal -> 409, like
    ``RunModeMismatchError``.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        super().__init__(
            f"Execution {blocking_execution_id!r} for workflow "
            f"{blocking_workflow_name!r} is paused and not accepting "
            f"submissions. Resume it to accept JOIN_EXISTING submissions, or "
            f"resubmit as STANDALONE to start a new execution."
        )


class SubmissionBlockedByOrphanedBacklogError(RuntimeError):
    """Raised when a JOIN_EXISTING submission targets an execution with a
    quarantined (orphaned-backlog) slot.

    A BATCHABLE slot key collapses the submission component, so a join would
    route the NEW submission's contributions straight into the quarantined
    slot, where the accept-partial resume would silently discard them.
    Engine-state refusal -> 409, like ``SubmissionToPausedExecutionError``.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        super().__init__(
            f"Execution {blocking_execution_id!r} for workflow "
            f"{blocking_workflow_name!r} has an orphaned backlog (a receiver "
            f"died still owing contributions) and is not accepting "
            f"JOIN_EXISTING submissions. Resume the execution to accept the "
            f"partial fill first, or resubmit as STANDALONE to start a new "
            f"execution."
        )


class LiveSubmissionWithSimOverridesUnacknowledgedError(ValueError):
    """Raised when a LIVE submission references devices with sim-direction overrides.

    The v3.4 12-row resolver warns whenever a LIVE submission encounters a
    device whose topology declares `sim_override=PURE_SIM` or
    `sim_override=DEVICE_SIM`. The "did you forget to switch out of sim?"
    gate. The submission is accepted only if the operator passes
    `acknowledge_warnings=True` (`--confirm` on the CLI).

    Carries the list of (device_name, sim_override, resolved_mode) tuples
    so surfaces can render the offending devices verbatim.

    Base class is ValueError (not RuntimeError) so the daemon route maps
    it to 422 (validation failure on the submission shape) rather than
    409 (runtime conflict). The plan's status table is the source of
    truth: LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED is 422.
    """

    _NAMES_INLINE_LIMIT = 5

    def __init__(
        self,
        devices: list[tuple[str, WorkflowRunMode, WorkflowRunMode]],
    ) -> None:
        self.devices = list(devices)
        all_names = [name for name, _, _ in self.devices]
        if len(all_names) > self._NAMES_INLINE_LIMIT:
            shown = ", ".join(all_names[:self._NAMES_INLINE_LIMIT])
            remaining = len(all_names) - self._NAMES_INLINE_LIMIT
            names = (
                f"{shown}, ... and {remaining} more (run `orca device list` "
                f"for the full set, or read `extras.devices` for the typed list)"
            )
        else:
            names = ", ".join(all_names)
        super().__init__(
            f"LIVE submission references {len(self.devices)} device(s) with "
            f"topology sim_override set: {names}. Resubmit with "
            "acknowledge_warnings=True (or `--confirm` via CLI/REST) if you "
            "intended this configuration."
        )


class RunModeMismatchError(RuntimeError):
    """Raised when a JOIN_EXISTING submission's run_mode differs from the live execution.

    Replaces the ``CONCURRENT_SUBMISSION_REFUSED`` refuse-all, which was
    lifted when device initialization became lazy. Every submission in
    one execution must share the same `WorkflowRunMode` because the
    execution's running threads share a single per-task ContextVar
    chain; mixing modes would let two threads in the same execution
    dispatch against different drivers. STANDALONE submissions are
    unaffected because each gets its own fresh execution with its own
    ContextVar seed.

    Carries the blocking execution's id, the existing run_mode, and the
    submitted run_mode so callers can render an actionable error.
    Multi-mode-per-execution would require dispatch rewiring and is
    explicitly deferred.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
        existing_run_mode: WorkflowRunMode,
        submitted_run_mode: WorkflowRunMode,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        self.existing_run_mode = existing_run_mode
        self.submitted_run_mode = submitted_run_mode
        super().__init__(
            f"JOIN_EXISTING submission with run_mode="
            f"{submitted_run_mode.name} cannot join execution "
            f"{blocking_execution_id!r} (workflow "
            f"{blocking_workflow_name!r}), which is running under "
            f"run_mode={existing_run_mode.name}. Resubmit with the "
            f"matching run_mode, or use batch_mode=STANDALONE to start "
            f"a fresh execution."
        )


class ConcurrentLiveSimRefusedError(RuntimeError):
    """Raised when a LIVE and a DEVICE_SIM execution would run concurrently.

    LIVE and DEVICE_SIM both dispatch over the wire to the same
    orca-client device connections. Running them at once would let a
    DEVICE_SIM execution switch a device to its sim backend on the device
    bridge while a LIVE execution drives the same device for real (or vice
    versa), knocking the live device offline. So the two cannot be active
    simultaneously. PURE_SIM never reaches the wire, so it is exempt and
    can run alongside anything.

    Unlike `RunModeMismatchError` (which fires when a JOIN_EXISTING
    submission's exact run_mode differs from the execution it tries to
    join), this fires across SEPARATE executions and only on the
    LIVE/DEVICE_SIM boundary: two concurrent LIVE executions are fine, and
    two concurrent DEVICE_SIM executions are fine. It is the
    cross-execution analogue, so it inherits `RuntimeError` like the other
    engine-state refusals and maps to 409.

    Carries the blocking execution's id + workflow name and the two run
    modes so the operator knows which run to wait on.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
        existing_run_mode: WorkflowRunMode,
        submitted_run_mode: WorkflowRunMode,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        self.existing_run_mode = existing_run_mode
        self.submitted_run_mode = submitted_run_mode
        super().__init__(
            f"Cannot start a {submitted_run_mode.name} execution while "
            f"execution {blocking_execution_id!r} (workflow "
            f"{blocking_workflow_name!r}) is running under "
            f"{existing_run_mode.name}. LIVE and DEVICE_SIM executions "
            f"cannot run at the same time because they drive the same "
            f"devices over the wire. Wait for the running execution to "
            f"terminate, then resubmit."
        )


# -- Unified DeviceRegistry --------------------------------------------------
# Composes topology + connection sources into a single read surface keyed
# by device name. Live state (is_connected, is_initialized) is queried on
# demand against the connection source or driver, never cached or persisted
# on registry fields.


class IDeviceConnectionSource(Protocol):
    """Source of connection-card information for the unified DeviceRegistry.

    Implementations describe whatever the deployment uses to learn about
    reachable devices. Source-available default: `NullDeviceConnectionSource` (always
    empty). A hosted deployment: `DeviceConnectionSource` wrapping the in-memory
    `DeviceConnectionTracker` plus the Postgres `RegisteredDevice` row.

    `is_connected` answers live; the registry never caches the result.
    `last_heartbeat` semantics (and the staleness threshold) are owned by
    the source implementation.

    A card also carries `device_is_connected` / `device_is_initialized` and the
    `device_link_mode` that names which driver answered: what the on-prem
    device bridge last reported about that device's own driver. The device
    bridge holds the driver, so it is the only party that can answer those. A
    source reporting nothing for a device does NOT mean "read the local driver
    instead": that is only true where no device bridge holds the device at all,
    and `DeviceLinkReader` owns the distinction. See its module docstring for
    the whole rule.

    `peek_reported_link` is the same answer without the rest of the card, and
    synchronous so the runtime's synchronous snapshot builders can source their
    flags from here instead of from a proxy's cache.
    """

    async def get_connection_card(self, name: str) -> ConnectionCard | None: ...

    def peek_reported_link(self, name: str) -> ReportedDeviceLink | None: ...
    async def list_connection_cards(self) -> list[ConnectionCard]: ...
    async def is_connected(self, name: str) -> bool: ...


class IDeviceRegistry(Protocol):
    """Unified read surface for device registry entries.

    Composes the topology (declared) and connection (reachable now) cards
    into one two-card view. Either card may be present alone; absence is
    the signal (no `unknown_to_topology` flag). The user mental model is: connections are the truth, topology is
    the declaration overlay.

    Live state (`is_connected`, `is_initialized`) is queried per call on
    `DeviceRegistryEntry`. The registry holds no state beyond pointers to
    the topology + connection sources.

    `assert_runnable` carries the mode validation for
    `submit_workflow(mode=)`. `DeviceController` validates against
    `effective_interfaces`. This Protocol is the integration point.
    """

    async def get(self, name: str) -> DeviceRegistryEntry | None: ...
    async def list_all(self) -> list[DeviceRegistryEntry]: ...
    async def assert_runnable(
        self, names: Iterable[str], mode: WorkflowRunMode,
    ) -> None: ...


class WorkflowDeviceMissingError(RuntimeError):
    """Raised when a workflow submission references devices not in topology.

    PURE_SIM mode: a device named in the workflow but absent from topology
    has no declaration to dispatch against. The error message lists every
    offending name (not just the first) so operators can fix the topology
    or the workflow in one pass.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"Devices not in topology: {missing}")


class WorkflowDeviceNotConnectedError(RuntimeError):
    """Raised when a workflow submission requires connections but lacks them.

    DEVICE_SIM and LIVE modes require both cards present and the device
    currently connected (per R1). Topology-only entries fail this check
    explicitly: declared but not reachable. The error message lists every
    offending name so operators can act on the full set.

    `undeclared_connected` lists devices that ARE connected but are not in
    topology. A common cause of this error is a name mismatch (the device
    connected under a different name than topology declares), so naming the
    undeclared connections turns a confusing "not connected" into an obvious
    typo to fix.
    """

    def __init__(
        self, missing: list[str], undeclared_connected: list[str] | None = None,
    ) -> None:
        self.missing = missing
        self.undeclared_connected = undeclared_connected or []
        message = f"Devices not currently connected: {missing}"
        if self.undeclared_connected:
            message += (
                f". Note: these connected devices are not declared in "
                f"topology: {self.undeclared_connected} -- possible name mismatch."
            )
        super().__init__(message)


class DeckLayoutRequiredError(RuntimeError):
    """Raised when a DEVICE_SIM / LIVE submission has unconfigured deck layouts.

    Every ``LiquidHandler`` in the topology must declare a non-None
    ``deck_layout`` and that name must resolve to an entry in its
    ``deck_layout_store`` before submit. Under PURE_SIM the check is
    skipped (sim runs against an empty driver state).

    ``missing_declaration`` names handlers whose constructor had
    ``deck_layout=None``. ``unresolved_layout`` names handlers whose
    declared layout could not be found in the per-device store.
    """

    def __init__(
        self,
        mode: "WorkflowRunMode",
        missing_declaration: list[str],
        unresolved_layout: list[tuple[str, str]],
    ) -> None:
        self.mode = mode
        self.missing_declaration = missing_declaration
        self.unresolved_layout = unresolved_layout
        parts: list[str] = []
        if missing_declaration:
            parts.append(
                f"liquid handlers without deck_layout: {missing_declaration}. "
                "Declare via LiquidHandler(name, ..., deck_layout='<name>')."
            )
        if unresolved_layout:
            pretty = [f"{name}.deck_layout={layout!r}" for name, layout in unresolved_layout]
            parts.append(
                f"declared layouts not in the per-device deck_layout_store: {pretty}. "
                "Seed via the stores factory at topology build time, or "
                "register at runtime via the deck-layout REST/MCP surface."
            )
        super().__init__(
            f"{mode.name} submissions require every liquid handler to declare a "
            f"deck_layout that resolves in its store. " + " ".join(parts)
        )


class OccupiedSlot(BaseModel):
    """One occupied start_location that blocks a submission.

    Carried in the `occupied` list of `StartLocationsOccupiedError` and
    serialized verbatim into the error envelope on the hosted wire so the
    operator sees exactly which location(s) to clear.

    ``source`` is narrowed to ``Literal["unknown"]``: the only call site
    cannot distinguish operator-placed vs prior-execution leftovers
    without provenance metadata on ``LabwareInstance``, and shipping the
    misleading wider hint is worse than shipping no hint. If/when
    provenance is added, widen the Literal back at the same time as the
    detection logic lands.
    """

    position_id: str
    existing_labware_name: str
    existing_template_name: str
    source: Literal["unknown"] = "unknown"


class StartLocationsOccupiedError(RuntimeError):
    """Raised at submit time when an entry thread's start_location is occupied.

    The pre-check runs after `_resolve_acquisitions` and before any thread
    starts. Without this check, `ExecutingLabwareThread.initialize_labware`
    would retry `DeviceBusyError` from the occupied location forever — the
    silent stall. Surfacing here turns it into a typed envelope the
    operator can act on (clear the location, then resubmit).

    Threads with a non-default spawn mode (e.g. reuse_existing) and threads
    whose acquisition was already resolved to an existing LabwareInstance
    are skipped.
    """

    def __init__(self, occupied: list[OccupiedSlot]) -> None:
        self.occupied = list(occupied)
        names = ", ".join(s.position_id for s in self.occupied)
        super().__init__(
            f"Cannot submit: {len(self.occupied)} start_location(s) occupied "
            f"by labware from a prior execution or operator placement: {names}. "
            f"Clear via `labware clear-submission`, `labware discharge`, or "
            f"`labware clear-all` before resubmitting."
        )


class ReuseThreadCannotBeEntryError(RuntimeError):
    """Raised at build time when a `start_reuse_existing` thread is registered
    via `wf.start()` instead of `wf.thread()`.

    `wf.start()` constructs the thread eagerly per submission via
    `build_entry_threads_for`. If a reuse-binding thread were registered
    that way, every submission would mint a fresh `LabwareThreadInstance`
    against the same bound labware -- multi-receiver, one-labware. Reuse
    threads must go through the auto-spawn slot machinery (`wf.thread()`)
    which guarantees exactly one receiver per slot.
    """

    def __init__(self, thread_name: str) -> None:
        self.thread_name = thread_name
        super().__init__(
            f"Thread '{thread_name}' has start_reuse_existing=True and cannot "
            f"be registered as a workflow entry via wf.start(). Use wf.thread() "
            f"so the auto-spawn slot machinery guarantees a single receiver "
            f"binds the persistent labware."
        )


class ImmovableThreadCannotBeEntryError(RuntimeError):
    """Raised at build time when a thread with `immovable=True` is registered
    via `wf.start()` instead of `wf.thread()`.

    An immovable thread asserts the engine will never move its labware
    off `start_location`. As a workflow entry, its first action against
    its own `start_location` would immediately fall into the
    unresolvable-deadlock declaration path the immovable flag is
    designed to fire -- the thread's own labware would be flagged as
    the blocker for the thread itself. Refused at build time so the
    operator catches the contradiction before submission.

    Symmetric with `ReuseThreadCannotBeEntryError` for the other
    thread-start intent that doesn't make sense as a workflow entry.
    """

    def __init__(self, thread_name: str) -> None:
        self.thread_name = thread_name
        super().__init__(
            f"Thread '{thread_name}' was registered as a workflow entry via "
            f"wf.start() with immovable=True. An immovable thread cannot "
            f"initiate a workflow because its first action against its own "
            f"start_location would deadlock against the immovable flag's own "
            f"contract. Wrap the thread in wf.thread() so an entry thread "
            f"spawns it, or drop immovable=True if the thread genuinely "
            f"needs to be an entry."
        )


class SpawnDidNotPlaceError(RuntimeError):
    """A spawn action returned without putting the thread's labware anywhere.

    An engine bug, not an operator one. Every spawn either places the labware
    (sim writes the slot, dispense advances the source, a LIVE manual place
    waits for the operator to register) or the thread was cooperatively
    stopped. Anything else means the ledger never saw the arrival, and letting
    the thread continue would run it against a driver deck that was never told
    about the plate -- surfacing much later as an unexplained pick.
    """

    def __init__(
        self,
        thread_name: str,
        labware_name: str,
        location: str,
        spawn: str,
        placement: str,
    ) -> None:
        self.thread_name = thread_name
        self.labware_name = labware_name
        self.location = location
        self.spawn = spawn
        self.placement = placement
        super().__init__(
            f"{spawn} returned for thread {thread_name!r} but {labware_name!r} "
            f"is still {placement} at {location!r}: the spawn never recorded an "
            f"arrival, so nothing placed the labware"
        )


class SpawnIncompatibleError(RuntimeError):
    """Raised when labware of the wrong template is offered at a location a
    thread is depending on.

    Two producers:

    - `ExecutingWorkflow._resolve_reuse_bind` raises when reuse-bind
      enters the location's `spawn_lock` and finds existing labware
      whose template name does not match `template.labware_template.name`.
      Operator pre-loaded the wrong trough; the binding refuses rather
      than shadowing.
    - `LabwareFacade.register` raises when a thread is waiting for a manual
      place at that slot and the operator offers a different template. The
      refusal lands on the operator's own call, before anything is written, so
      they can correct it -- rather than the thread failing behind them.

    A typed envelope is friendlier than the alternative -- falling back to
    create-fresh would silently shadow the operator's labware, and stalling
    on `DeviceBusyError` would hide the cause.
    """

    def __init__(
        self,
        location: str,
        expected_template: str,
        actual_template: str,
    ) -> None:
        self.location = location
        self.expected_template = expected_template
        self.actual_template = actual_template
        super().__init__(
            f"Location '{location}' holds labware with template "
            f"'{actual_template}' but reuse-bind expected template "
            f"'{expected_template}'. Clear the location or fix the workflow."
        )


class TransitLabwareMissingDeckSiteError(RuntimeError):
    """Raised when a transit labware moves onto a multi-site deck without a valid site.

    On a multi-site device (a liquid handler with a derived deck) every transit
    labware needs its own named deck site, regardless of how many labware the
    action involves. A transit input the action moves in gets its site from the
    action's ``deck_positions``; the entry must resolve to a REAL deck site
    (derived from the deck layout) on that device. With no entry, or one that
    resolves to no site, the engine cannot choose where the plate lands and the
    action cannot address it. ``deck_positions`` values are BARE site names
    (``carrier-7-0``, ``C2-slot``); the engine prefixes the action's device, so
    a device-prefixed value double-prefixes and matches nothing. Residents are
    exempt: they are sited by their thread ``start=`` at a child site (the
    device-prefixed ``<device>/<site>`` form) and never move in.
    """

    def __init__(
        self,
        thread_name: str,
        device_location: str,
        template_name: str,
        available_sites: List[str],
        action_command: str,
        declared_site: Optional[str] = None,
    ) -> None:
        self.thread_name = thread_name
        self.device_location = device_location
        self.template_name = template_name
        self.available_sites = available_sites
        self.action_command = action_command
        self.declared_site = declared_site
        # A site id carries its device and deck_positions does not, so listing the
        # ids hands the operator the double-prefixed value the hint below warns about.
        prefix = f"{device_location}/"
        pasteable = [
            s[len(prefix):] if s.startswith(prefix) else s for s in available_sites
        ]
        sites = ", ".join(pasteable) if pasteable else "none"
        if declared_site is None:
            reason = (
                f"with no deck_positions entry. Every transit labware on a multi-site "
                f"deck needs its own named deck site; without one the engine cannot "
                f"choose where '{template_name}' lands. Add a deck_positions entry "
                f"mapping '{template_name}' to a deck site, on the @orca.action "
                f"for '{action_command}'."
            )
        else:
            looked_for = f"{device_location}/{declared_site}"
            hint = ""
            if declared_site.startswith(f"{device_location}/"):
                hint = (
                    f" deck_positions values are BARE site names (e.g. "
                    f"'carrier-7-0', 'C2-slot'); the action already targets "
                    f"'{device_location}', so the engine prefixes the device for "
                    f"you. '{declared_site}' is already device-prefixed, so it "
                    f"resolved to '{looked_for}' and matched no site. Drop the "
                    f"'{device_location}/' prefix. (The device-prefixed "
                    f"'<device>/<site>' form is only for thread start=/end=.)"
                )
            elif "/" in declared_site:
                hint = (
                    f" deck_positions values are BARE site names (e.g. "
                    f"'carrier-7-0', 'C2-slot') with no '/'; the action already "
                    f"targets '{device_location}', so the engine prefixes the "
                    f"device for you and '{declared_site}' resolved to "
                    f"'{looked_for}', matching no site. deck_positions cannot "
                    f"address another device's sites. (The device-prefixed "
                    f"'<device>/<site>' form is only for thread start=/end=.)"
                )
            reason = (
                f"via deck_positions site '{declared_site}' (the engine looked for "
                f"'{looked_for}'), which is not a deck site on that device. Map "
                f"'{template_name}' to a real deck site, on the @orca.action for "
                f"'{action_command}'.{hint}"
            )
        super().__init__(
            f"Thread '{thread_name}' moves labware '{template_name}' onto multi-site "
            f"device '{device_location}' {reason} Sites deck_positions accepts "
            f"here: {sites}."
        )


class SpawnContextUnavailableError(RuntimeError):
    """Raised when a reuse-bind dispatch fires but the ExecutingWorkflow was
    constructed without `labware_store` + `system` references.

    Three orca-core test fixtures construct ExecutingWorkflow without these
    refs (they predate the start-location checks); they keep working because they
    never use reuse-existing threads. Encountering this error means a
    reuse-existing thread is in play in a context that didn't plumb the
    refs through -- always a wiring bug, not an operator action.
    """


class ActiveExecutionRefusedError(RuntimeError):
    """Raised by the operator clear surface when a non-terminal execution
    blocks the clear and the caller did not pass `force=True`.

    Carries the offender (submission_id or labware_id) so the hosted wire
    layer can surface a typed CONFLICT envelope (matches the pattern
    set by `StartLocationsOccupiedError` and `SpawnIncompatibleError`).
    """

    def __init__(
        self,
        scope: Literal["submission", "labware", "all"],
        *,
        submission_id: str | None = None,
        labware_id: str | None = None,
    ) -> None:
        self.scope = scope
        self.submission_id = submission_id
        self.labware_id = labware_id
        if scope == "submission":
            assert submission_id is not None
            detail = f"submission '{submission_id}' is part of an active execution"
        elif scope == "labware":
            assert labware_id is not None
            detail = f"labware '{labware_id}' is referenced by an active thread"
        else:
            detail = "at least one active execution exists"
        super().__init__(
            f"Refusing clear: {detail}. Pass force=True to override."
        )


class LocationReservedError(RuntimeError):
    """An operator asked to put labware at a position another thread has
    reserved.

    A reservation is a thread's claim on somewhere to put a plate down, granted
    before the arm leaves, so a slot can be spoken for while it still looks
    empty. Writing labware onto one means the reserving move arrives to find
    the site taken and refuses, hours after the operator moved on.

    Two holders reach this refusal and they mean opposite things, so the
    message says which:

    - **A real plate is on its way here.** Nothing is wrong and nothing needs
      clearing. It lands, the operator takes it off, and the position is then
      free for what they were trying to put down. ``inbound_from`` says where
      it is now.
    - **Another placement was asked for here.** A person is already holding an
      instruction for this position. Doing that one is what frees it, or the
      claim can be cancelled.

    ``inbound_labware`` names the labware in both cases; ``awaiting_operator``
    is what says which of the two it is.

    A thread does NOT block the labware it is itself carrying: an operator
    finishing that thread's move by hand is exactly the case ``edit-location``
    exists for.
    """

    def __init__(
        self, position_id: str, reservation_id: str,
        holder_thread_id: str | None, holder_thread_name: str | None = None,
        inbound_labware: str | None = None, inbound_from: str | None = None,
        awaiting_operator: bool = False,
    ) -> None:
        self.position_id = position_id
        self.reservation_id = reservation_id
        self.holder_thread_id = holder_thread_id
        self.holder_thread_name = holder_thread_name
        self.inbound_labware = inbound_labware
        self.inbound_from = inbound_from
        self.awaiting_operator = awaiting_operator
        holder = (
            f"thread '{holder_thread_name or holder_thread_id}'"
            if holder_thread_id is not None else "no thread"
        )
        super().__init__(
            self._explain(position_id, reservation_id, holder)
        )

    def _explain(
        self, position_id: str, reservation_id: str, holder: str,
    ) -> str:
        if self.awaiting_operator:
            expected = (
                f" for {self.inbound_labware}" if self.inbound_labware else ""
            )
            return (
                f"{position_id} is already claimed{expected} by {holder}, "
                f"which is waiting for someone to place labware there. Doing "
                f"that placement frees the position; if it is not going to "
                f"happen, clear the claim first: `orca reservation cancel "
                f"{reservation_id}`, or abort the thread holding it."
            )
        if self.inbound_labware is not None:
            origin = f" from {self.inbound_from}" if self.inbound_from else ""
            # This refusal usually reaches someone who has already put the
            # plate down, and the engine cannot see labware nobody registered.
            # Telling them to wait without telling them to take theirs back
            # lands the arriving plate on top of it.
            return (
                f"{self.inbound_labware} is on its way to {position_id}"
                f"{origin}, carried by {holder}. Take your labware back off "
                f"{position_id} if you have already put it there, let that one "
                f"land, remove it, and then place yours. Cancelling the claim "
                f"({reservation_id}) sends that plate to a position you have "
                f"filled, so only do it if you are also stopping the thread."
            )
        # A plate already standing there cannot wait for the holder to finish:
        # it is what stops the holder finishing. Name a way out that works.
        return (
            f"{position_id} is reserved by {holder} ({reservation_id}). Name a "
            f"different position, or abort the thread holding it. `orca "
            f"reservation cancel {reservation_id}` frees the spot only until "
            f"the holder asks for it again."
        )


class MoverHoldsNothingError(RuntimeError):
    """Raised when a mover-hold release names a mover the record says is empty.

    Its own type because the answer differs from every other miss: nothing is
    wrong, the jaws were already free, and the operator's next step is to look
    at what actually refused them. A state conflict rather than a missing key,
    so it sits with ``LocationReservedError`` and its message reaches the
    operator unquoted.
    """

    def __init__(self, mover_name: str) -> None:
        self.mover_name = mover_name
        super().__init__(
            f"The record has {mover_name!r} holding nothing, so there is no "
            f"hold to release."
        )


class LabwareNotFoundError(KeyError):
    """Raised when an operator clear / discharge targets a labware id that
    does not exist in any runtime store.

    Subclasses `KeyError` so older callers that catch `KeyError` keep
    working; the typed subclass lets the hosted wire layer pick a
    LABWARE_NOT_FOUND envelope code without isinstance-on-KeyError.
    """

    def __init__(self, labware_id: str) -> None:
        self.labware_id = labware_id
        super().__init__(f"No labware with id '{labware_id}'")


# -- IOpsHistoryFacade -------------------------------------------------------


class IOpsHistoryFacade(ABC):
    """Read-only access to the per-execution OpsHistory archive.

    No write methods: records are emitted by the action-execution pipeline
    through TrackingContext. Operators query past executions via ``list`` /
    ``search``; REST + MCP route through this facade so the source-available in-memory
    impl and a hosted deployment's Postgres-indexed JSONL impl share the same surface.
    """

    @abstractmethod
    async def list(self, execution_id: str) -> List[TrackingRecord]: ...

    @abstractmethod
    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> List[tuple[str, TrackingRecord]]: ...


# -- DeviceInvocationResult (wrapper for device execute/invoke) -------------


@dataclass(frozen=True)
class DeviceInvocationResult:
    """Uniform return type for `device.execute` / `device.invoke`.

    JSON-serializable for CLI `--json` output, REST responses, MCP tool returns.
    """
    success: bool
    value_type: str                   # "None" | "int" | "float" | "str" | "bool" | "json"
    value: str | None                 # stringified; None when value_type == "None"
    duration_seconds: float
    device_name: str
    command_or_capability: str


# -- ISystemRuntime ----------------------------------------------------------


@runtime_checkable
class IExecutionIterator(Protocol):
    """Narrow surface over the runtime's live-execution registry.

    Decouples `LabwareFacade` (and other operator-surface helpers) from the
    full `ISystemRuntime` so they can iterate active executions without
    pulling in submission / facade dependencies.
    """

    def iter_executions(self) -> Iterator[Execution]: ...


@runtime_checkable
class ISystemRuntime(Protocol):
    """The operator contract for a running Orca system.

    UIs program against this Protocol. Structural typing means any class
    satisfying the surface is acceptable -- including future decorators like
    a hosted deployment's `OrgScopedRuntime`.
    """

    # -- Lifecycle --
    async def start(self) -> None: ...
    async def shutdown(self, *, confirm: bool = False) -> None: ...

    # -- What is stopping the run --
    async def blockers(self) -> list[Blocker]: ...

    # -- Execution submission + query --
    async def submit_workflow(
        self,
        workflow_name: str,
        variables: dict[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
    ) -> ExecutionRecord: ...

    async def submit_method(
        self,
        workflow_name: str,
        method_name: str,
        labware_start: dict[str, str],
        labware_end: dict[str, str],
        variables: dict[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        acknowledge_warnings: bool = False,
    ) -> ExecutionRecord: ...

    async def wait(self, execution_id: str) -> ExecutionRecord: ...
    def get_execution(self, execution_id: str) -> ExecutionRecord: ...
    def get_execution_detail(self, execution_id: str) -> ExecutionDetail: ...
    def get_execution_status(self, execution_id: str) -> ExecutionStatus: ...
    def list_executions(self) -> list[ExecutionRecord]: ...
    def iter_executions(self) -> Iterator[Execution]: ...

    async def stop_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> StopOutcome: ...

    async def abort_execution(self, execution_id: str) -> None: ...

    def remove_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> None: ...

    # -- Operator manual steps --
    def list_pending_manual_steps(
        self, execution_id: str | None = None,
    ) -> list[PendingManualStepRecord]: ...

    async def confirm_manual_step(
        self, execution_id: str, step_id: str,
    ) -> None: ...

    # -- Recoverable-timeout operator decisions --
    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float,
    ) -> None: ...
    def recoverable_timeout_abort(
        self, incident_id: str, operator: str, reason: str,
    ) -> None: ...
    def recoverable_timeout_mark_complete(
        self, incident_id: str, operator: str, reason: str,
    ) -> None: ...

    # -- Sub-facades --
    @property
    def variables(self) -> IVariableFacade: ...
    @property
    def labware(self) -> ILabwareFacade: ...
    async def unsettled_state(self) -> list[UnsettledSubject]:
        """Everything the record cannot answer, as one worklist."""
        ...
    @property
    def devices(self) -> IDeviceFacade: ...
    @property
    def threads(self) -> IThreadFacade: ...
    @property
    def registry(self) -> IRegistryFacade: ...
    @property
    def system(self) -> ISystem:
        """The live System (resources, devices, pools).

        Exposed so the Operations layer can resolve live device/pool
        objects for ad-hoc code injection (insert_action / insert_method);
        the resolver is built from this and handed to compile_*_code.
        Read-only accessor; mutation goes through the facades.
        """
        ...
    @property
    def incidents(self) -> IIncidentFacade: ...
    @property
    def teachpoints(self) -> ITeachpointFacade: ...
    @property
    def deck_layouts(self) -> IDeckLayoutFacade: ...
    @property
    def submissions(self) -> ISubmissionFacade: ...

    # -- Deployment-scoped Services (CRUD facades live on IDeploymentRegistries,
    # not here; the runtime exposes these so surfaces resolve and persist
    # through the SAME instance the runtime itself uses) --
    @property
    def labware_catalog_store(self) -> LabwareCatalogService: ...
    @property
    def execution_records(self) -> ExecutionRecordService:
        """The Service terminal executions are persisted through and read back from.

        On the contract because the get/list/detail execution Operations take
        their terminal-record fallbacks from here: a surface that only sees
        the live runtime reports a completed execution as missing once the
        runtime evicts it.
        """
        ...
    @property
    def access_config_store(self) -> IAccessConfigStore: ...
    @property
    def profile_store(self) -> IDeploymentProfileStore: ...

    # -- Device-registry split (topology + gateway, unioned by DeviceFacade) --
    @property
    def topology(self) -> ITopologyRegistry: ...
    @property
    def gateway(self) -> IGatewayRegistry: ...
    @property
    def device_registry(self) -> IDeviceRegistry: ...
    @property
    def ops_history(self) -> IOpsHistoryFacade: ...

    # -- Events --
    def register_sink(self, sink: IEventSink) -> None: ...
    def get_events_since(self, timestamp: float | None) -> list[RuntimeEvent]: ...
    def get_events_for_execution(self, execution_id: str) -> list[RuntimeEvent]: ...
    def subscribe_events(
        self, *,
        since: float | None = None,
        execution_id: str | None = None,
        entity_type: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]: ...

    # -- Plugins --
    def register_plugin(self, plugin: OrcaPlugin) -> None: ...
    def unregister_plugin(
        self, plugin_type: type[OrcaPlugin], *, confirm: bool = False,
    ) -> None: ...
    def disable_plugin(
        self, plugin_type: type[OrcaPlugin], *, confirm: bool = False,
    ) -> None: ...
    def enable_plugin(self, plugin_type: type[OrcaPlugin]) -> None: ...
    def list_plugins(self) -> list[PluginSnapshot]: ...
    def get_plugin(self, plugin_type: type[_P]) -> _P: ...
    def list_plugin_commands(self) -> list[PluginCommand]: ...
    async def execute_plugin_command(self, name: str, args: list[str]) -> str: ...

    # -- Introspection (drives CLI/REST/MCP danger prompts) --
    def describe_action(self, action_name: str) -> ActionDescriptor: ...
    def list_actions(self) -> list[ActionDescriptor]: ...
