"""Facade interfaces: the namespaced operator surfaces of the runtime, and the models and protocols they are built on."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Callable, List, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, Teachpoint

from orca.gateway.device_fault import DeviceFault
from orca.state.records import TrackingRecord
from orca.runtime.execution import StopOutcome
from orca.runtime.execution_record import ExecutionRecord
from orca.runtime.incident_store import (
    IncidentCategory,
    SystemIncident,
)
from orca.state.ops_store import OpsHistorySearchQuery
from orca.workflow_models.labware_group import LabwareGroup
from orca.runtime.move_parameter_models import (
    LabwareGripProfile,
    TransporterMoveDefaults,
)
from orca.workflow_models.submission import BatchMode
from orca.resource_models.resource_registry import IResourceRegistry
from orca.system.system_interface import DeckComparison, ISystem
from orca.state.mounted import MountedTips
from orca.state.contents import ContentsResolution
from orca.runtime.status_models import (
    CommandDescriptor,
    DeviceIntrospection,
    DeviceFaultSummary,
    DeviceSnapshot,
    LabwareSnapshot,
    LabwareTemplateSnapshot,
    LocationEvent,
    LocationSnapshot,
    MethodTemplateSnapshot,
    ReservationSnapshot,
    ResourcePoolSnapshot,
    SubmissionCloseResult,
    SubmissionSnapshot,
    SystemInfoSnapshot,
    ThreadSnapshot,
    ThreadTemplateSnapshot,
    TipStateSnapshot,
    WellVolumesSnapshot,
    MoverSnapshot,
    TransporterSnapshot,
    EngineDeviceState,
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
        callback: Callable[[ISystem, str], None],
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

    def skip_method(self, thread_id: str, *, method_name: str) -> None: ...

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
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def set_global(
        self, name: str, value: OptionValue, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    def set_submission(
        self, name: str, value: OptionValue, execution_id: str,
        submission_id: str, *, reason: str | None = None, confirm: bool = False,
    ) -> None:
        """Write the layer that outranks the per-execution partition.

        Values supplied on a submit land here, so this is the only write that
        changes what an already-submitted thread resolves.
        """

    @abstractmethod
    def unset(
        self, name: str, execution_id: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    def unset_submission(
        self, name: str, execution_id: str, submission_id: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Drop one submission's override so it falls through to the layers below."""

    @abstractmethod
    async def unset_global(
        self, name: str, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...


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
    that information. Used end-to-end: facade -> a hosted deployment's REST/CLI ->
    cloud client; serialized verbatim into the response envelope.
    """

    cleared: list[str]
    preserved_reuse_bound: list[str]


class ILabwareFacade(ABC):
    """Labware identity and location.

    Wraps both `ILabwareLocationService` (location tracking) and
    `ILabwareService` (identity by id/barcode). All methods are async for
    uniformity with the async labware service.

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
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def set_carry_override(
        self, labware_id: str,
        patch: MoveParameterPatch,
        clear: Sequence[MoveParameterField] = (),
        *, reason: str | None = None, confirm: bool = False,
    ) -> MoveParameterPatch: ...

    @abstractmethod
    async def clear_carry_override(
        self, labware_id: str, *, reason: str | None = None, confirm: bool = False,
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
        reason: str | None = None,
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
        reason: str | None = None, confirm: bool = False,
    ) -> ClearSubmissionResult:
        """Remove non-reuse-bound labware associated with a submission.

        Walks every thread tied to ``submission_id``; for each one whose
        thread template does NOT declare ``end_leave_in_place`` (or
        equivalently is not a reuse-bound persistent labware path),
        clears the labware from its current Location, ``system.labwares``,
        and the labware service. Reuse-bound labware is SKIPPED so deck-
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
    def tell_threads_to_look_again(self) -> None:
        """Wake every live thread to re-read where its labware is."""

    @abstractmethod
    async def discharge_labware(
        self, labware_id: str, *, force: bool = False,
        reason: str | None = None, confirm: bool = False,
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
        reason: str | None = None, confirm: bool = False,
    ) -> list[str]:
        """Panic button: clear every Location and remove every labware.

        Refuses if any active execution exists unless ``force=True``.
        Returns the list of cleared labware_ids.
        """
        ...


# -- IDeviceFacade -----------------------------------------------------------


class IDeviceFacade(ABC):
    """Direct device command invocation outside of workflow actions.

    A command by name goes through the operator runner; `command_name` says
    what a name listed by `get_supported_commands` (e.g. `shaker.shake`) sends.

    Commands that would contend with an in-progress action refuse by default;
    `--force` (confirm=True in the concrete impl) bypasses the check with an
    audible warning.

    The lifecycle is four separable steps: `connect` opens the link,
    `initialize` makes the device ready to command, the driver's own `home`
    moves it (sent by name, since homing has no verb of its own
    here), and `disconnect` hands it back. They stay separate on purpose, so
    that bringing a device up is never a request to move it: an arm that has
    not homed since power-on refuses the first move that needs a known
    position, and that refusal is the prompt to home deliberately. The
    exception is a driver whose vendor library offers only a compound bring-up,
    which homes inside `initialize` because it has no other way in. Of the
    three verbs this facade owns, only `connect` is free of consequence, so it
    alone is undecorated.
    """

    @abstractmethod
    async def get_device_status(self, device_name: str) -> DeviceSnapshot: ...

    @abstractmethod
    def get_supported_commands(self, device_name: str) -> list[CommandDescriptor]: ...

    @abstractmethod
    def get_device_introspection(self, device_name: str) -> DeviceIntrospection: ...

    @abstractmethod
    def command_name(self, device_name: str, name: str) -> str:
        """The command `name` sends: an orca-side namespace (`shaker.shake`) is dropped, a vendor prefix kept."""

    @abstractmethod
    async def after_lifecycle(self, device_name: str, command: str) -> None:
        """Bring a liquid handler's deck in line after an initialize, connect or disconnect."""

    @abstractmethod
    async def reconcile_deck(
        self, device_name: str, *,
        reason: str | None = None,
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
        self, device_name: str, *, reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """Claim a device for hands-on work until it is released.

        While held, workflow dispatch on the device, and moves into or out of
        it, raise ``DeviceUnderExternalControlError``.
        """
        ...

    @abstractmethod
    async def release_external_control(
        self, device_name: str, *, reason: str | None = None, confirm: bool = False,
    ) -> None:
        """Hand the device back to the workflow. Idempotent."""
        ...

    @abstractmethod
    async def clear_fault(
        self, device_name: str, *, reason: str | None = None, confirm: bool = False,
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
        *, head96: dict[int, tuple[str, str]] | None = None, reason: str | None = None, confirm: bool = False,
    ) -> None:
        """State what each head is carrying: `by_channel` for the channel head, `head96` for the 96 head.
        Absolute: a channel left out is recorded as carrying nothing."""
        ...

    @abstractmethod
    async def confirm_mounted_tips(
        self, device_name: str, *, reason: str | None = None, confirm: bool = False,
    ) -> None:
        """Agree with what the record already says the head is carrying."""
        ...

    @abstractmethod
    async def compare_deck(self, device_name: str) -> DeckComparison:
        """What this liquid handler's deck holds, against the ledger.

        Never changes the ledger, and files nothing. A driver world only behind
        the record is rebuilt first. Refuses non-liquid-handler devices and ones
        with no deck layout configured.
        """
        ...


# -- IThreadFacade -----------------------------------------------------------


class IThreadFacade(ABC):
    """Per-thread control: inspection, pause/resume/recover, and mutation.

    `recover` applies a `RecoveryDecision` (RETRY / RETRY_ACTION / CONTINUE /
    ABORT_ACTION / ABORT_METHOD / ABORT_THREAD) to an error-paused thread.
    RETRY repeats the step that failed: inside an action, the failed device
    call, with the action body suspended. RETRY_ACTION re-runs the whole action
    and is valid only where the action's own code failed. CONTINUE is valid only while an action is
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
        *, reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def resume(
        self, execution_id: str, thread_id: str,
        *, reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def pause_all(
        self, execution_id: str,
        *, reason: str | None = None,
        confirm: bool = False,
    ) -> dict[str, int]: ...

    @abstractmethod
    async def resume_all(
        self, execution_id: str,
        *, reason: str | None = None,
        confirm: bool = False,
    ) -> dict[str, int]: ...

    @abstractmethod
    async def recover(
        self, execution_id: str, thread_id: str, decision: RecoveryDecision, *,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...

    # Mutation ops (require PAUSED thread; enforced by MutationCoordinator)

    @abstractmethod
    async def skip_method(
        self, execution_id: str, thread_id: str, *,
        method_name: str,
        reason: str | None = None,
        confirm: bool = False,
    ) -> None:
        """`reason` is consumed by the @dangerous audit trail on the concrete
        facade; implementations may ignore it in their body."""
        ...

    @abstractmethod
    async def abort_method(
        self, execution_id: str, thread_id: str, *,
        method_name: str,
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
        reason: str | None = None,
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
    def list_transporters(self) -> list[TransporterSnapshot]: ...

    @abstractmethod
    async def engine_state(self, name: str) -> EngineDeviceState | None:
        """The engine's state for a device or transporter, or None for a name it does not hold."""

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

        Used by wire callers (REST `thread_insert_method` with
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

        Used by wire callers (REST `method_execute_standalone`,
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
        reason: str | None = None,
        confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def acknowledge_all(
        self, *,
        category: IncidentCategory | None = None,
        reason: str | None = None,
        confirm: bool = False,
    ) -> int: ...

    # -- Recoverable-timeout operator decisions --
    @abstractmethod
    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float, *,
        reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def recoverable_timeout_abort(
        self, incident_id: str, operator: str, *, reason: str, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def recoverable_timeout_mark_complete(
        self, incident_id: str, operator: str, *, reason: str, confirm: bool = False,
    ) -> bool:
        """Returns True when the record was left as it is because the labware has moved since the command."""
        ...


# -- IAccessConfigFacade -----------------------------------------------------


class IAccessConfigFacade(ABC):
    """Operator-facing CRUD for named approach/retract access patterns.

    Access configs are the global registry of named patterns that
    teachpoints reference. Mutation surface used by REST; the runtime
    itself reads through the underlying `IAccessConfigStore` directly.

    Reads pass through. A delete raises `AccessConfigInUseError` from
    `orca.runtime.access_config_store` while a teachpoint still names the config.
    """

    @abstractmethod
    async def get(self, name: str) -> AccessConfig | None: ...

    @abstractmethod
    async def list(self) -> List[AccessConfig]: ...

    @abstractmethod
    async def add(
        self, config: AccessConfig, *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, config: AccessConfig, *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(
        self, name: str, *, reason: str | None = None, confirm: bool = False,
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
        reason: str | None = None,
        confirm: bool = False,
    ) -> TransporterMoveDefaults: ...

    @abstractmethod
    async def reset(
        self, transporter_name: str, *, reason: str | None = None, confirm: bool = False,
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
        reason: str | None = None,
        confirm: bool = False,
    ) -> LabwareGripProfile: ...

    @abstractmethod
    async def reset(
        self, labware_type: str, *, reason: str | None = None, confirm: bool = False,
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
    system rebuild picks up the new layout. REST
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
        *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, device_id: str, name: str, config: DeckLayoutConfig,
        *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(
        self, device_id: str, name: str,
        *, reason: str | None = None, confirm: bool = False,
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
        *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, device_id: str, teachpoint: Teachpoint,
        *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(
        self, device_id: str, position_id: str,
        *, reason: str | None = None, confirm: bool = False,
    ) -> bool: ...

    @abstractmethod
    async def set_taught_with(
        self, device_id: str, position_id: str, labware_type: str | None,
        *, reason: str | None = None, confirm: bool = False,
    ) -> Teachpoint: ...

    @abstractmethod
    async def apply_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        patch: MoveParameterPatch,
        clear: Sequence[str] = (),
        *, reason: str | None = None, confirm: bool = False,
    ) -> Teachpoint: ...

    @abstractmethod
    async def clear_labware_override(
        self, device_id: str, position_id: str, labware_type: str,
        *, reason: str | None = None, confirm: bool = False,
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
        sim_speedup: float | None = None,
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
        self, execution_id: str, *, reason: str | None = None, confirm: bool = False,
    ) -> SubmissionCloseResult: ...

    @abstractmethod
    async def submit_method(
        self, *,
        workflow_name: str,
        method_name: str,
        labware_start: Mapping[str, str],
        labware_end: Mapping[str, str],
        variables: Mapping[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        mode: WorkflowRunMode | None = None,
        event_hooks: Sequence[str] | None = None,
        sim_speedup: float | None = None,
        confirm: bool = False,
    ) -> ExecutionRecord: ...

    @abstractmethod
    async def stop_execution(
        self, execution_id: str, *,
        abort: bool = False, reason: str | None = None, confirm: bool = False,
    ) -> StopOutcome: ...

    @abstractmethod
    def remove_execution(
        self, execution_id: str, *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def confirm_manual_step(
        self, execution_id: str, step_id: str, *,
        reason: str | None = None, confirm: bool = False,
    ) -> None: ...


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
        self, profile: DeploymentProfile, *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def update(
        self, profile: DeploymentProfile, *, reason: str | None = None, confirm: bool = False,
    ) -> None: ...

    @abstractmethod
    async def delete(self, name: str, *, reason: str | None = None, confirm: bool = False) -> bool: ...


# -- IOpsHistoryFacade -------------------------------------------------------


class IOpsHistoryFacade(ABC):
    """Read-only access to the per-execution OpsHistory archive.

    No write methods: records are emitted by the action-execution pipeline
    through TrackingContext. Operators query past executions via ``list`` /
    ``search``.
    """

    @abstractmethod
    async def list(self, execution_id: str) -> List[TrackingRecord]: ...

    @abstractmethod
    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> List[tuple[str, TrackingRecord]]: ...
