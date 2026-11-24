from abc import ABC, abstractmethod
import logging
import asyncio
from typing import AsyncGenerator, Awaitable, Callable, Dict, List, Optional, NoReturn

from orca.events.custom_emit import EventEmitter
from orca.events.event_bus_interface import IEventBus
from orca.events.event_channel import EventChannel, EventChannelRegistry
from orca.events.execution_context import (
    ManualInterventionContext,
    ThreadExecutionContext,
    WorkflowExecutionContext,
)
from orca.config import CoordinationConfig
from orca.resource_models.device_error import LabwareDischargedError, PlateDispenseError
from orca.resource_models.labware import AnyLabwareTemplate, LabwareInstance, LabwareTemplate
from orca.resource_models.capacity import CapacityExceededError
from orca.workflow_models.labware_state import (
    ILabwareRegistry,
    IRegisteredThread,
    LabwareSlot,
    LabwareState,
)
from orca.gateway.controller.exceptions import DeviceOfflineError
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.location import Location
from orca.resource_models.tracked_lock import LockWait, current_lock_wait
from orca.system.reservation_manager.errors import (
    AcquisitionYieldRequested,
    ActionContinuedContext,
    ActionFailedContext,
    AutoSpawnFailedDetail,
    CoLabwareTimeoutDetail,
    IThreadIncidentDeclarer,
    MoveAbandonedError,
    MoveContinuedContext,
    NoRouteError,
    MoveFailedContext,
    ThreadDiedContext,
    UnresolvableDeadlockError,
)
from orca.system.system_map import RouteGraphMissingTeachpointError
from orca.workflow_models.actions.move_handler import MoveHandler
from orca.workflow_models.thread_status_recorder import IThreadStatusRecorder
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver, UnresolvedLocationAction
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.actions.operation_recovery import (
    OperationDecisionSignal,
    UnsavedResult,
    device_op_recovery_handler,
)
from orca.resource_models.error_policy_overrides import (
    OverrideWithPauseError,
    PausesUnderAnyPolicyError,
    WrongPartnerPlateError,
)
from orca.workflow_models.actions.executable_location_action import (
    ExecutableLocationAction,
)
from orca.workflow_models.actions.move_action import MoveAction
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.event_step import BranchStepTemplate, WaitStepTemplate
from orca.workflow_models.interfaces import ILabwareThread, IMethod
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.async_util import drain_cancelled_waiters
from orca.workflow_models.labware_threads.co_labware_coordinator import (
    CoLabwareCoordinator,
    CoLabwareWaitOutcome,
)
from orca.workflow_models.labware_threads.i_thread_context import IMySlotView, IThreadContext
from orca.resource_models.location_history import LocationHistory
from orca.workflow_models.actions.location_action import ResidencyCheck
from orca.workflow_models.labware_threads.reservation_holdover import ReservationHoldover
from orca.workflow_models.labware_threads.template_dispatch import schedule_action_template
from orca.workflow_models.labware_threads.thread_state_machine import (
    ThreadEvent,
    ThreadStateMachine,
)
from orca.state.records import ObservationGapCause
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.state.placement import PlacementState
from orca.workflow_models.acquisition import StartDecision, StartFacts, decide_start
from orca.resource_models.command_effects import SystemCommandPipeline
from orca.workflow_models.merge_lane import DroppedAnchorInsert, MergeLane
from orca.workflow_models.spawn_actions import (
    DispenseSpawn,
    ManualPlaceSpawn,
    RelocateByHandSpawn,
    SpawnAction,
    SpawnWaitHooks,
    arrival_mechanism_for,
    select_end_spawn_action,
    select_spawn_action,
)
from orca.workflow_models.method import ExecutingMethod, MethodInstance, SharedRendezvousResolved
from orca.workflow_models.method_template import IMethodTemplate, JoinTemplate, MethodTemplate
from orca.workflow_models.shared_action_coordination import (
    ActionResolution,
    SharedActionCoordination,
)
from orca.workflow_models.status_enums import ESCALATION_ORDER, HONOURED_DECISIONS, FailurePolicy, LabwareThreadStatus, MethodStatus, PauseSite, RecoveryDecision
from orca.workflow_models.status_manager import StatusManager

from orca.workflow_models.group_execution_context import GroupExecutionContext
from orca.modes.run_modes import (
    RunningThread,
    WorkflowRunMode,
    current_execution_id,
    current_run_mode,
    current_thread,
)
from orca.runtime.recoverable_timeout import recoverable_timeout_coordinator
from orca.modes.sim_pacing import SimPacer, current_sim_pacer, pacer_for_run_mode
from orca.workflow_models.workflows.registry_interfaces import IMethodRegistry
from orca.variables.variable_store import IVariableResolver, NullVariableResolver
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.actions.method_action_factory import MethodActionFactory
from orca.workflow_models.workflows.workflow_registry import ExecutingMethodRegistry, IExecutingMethodRegistry, ThreadRegistry
from orca.workflow_models.errors import CoLabwareLostError, AutoSpawnFailedError, SpawnDidNotPlaceError, TransitLabwareMissingDeckSiteError

AutoSpawnCallback = Callable[[str, ExecutingMethod, WorkflowRunMode], Awaitable[None]]
CapacityPrecheckCallback = Callable[[str, GroupExecutionContext | None, ExecutingMethod], Awaitable[None]]
"""Side-effect-free pre-check used before any spawn commits in a multi-input
action. Raises CapacityExceededError (and emits the REJECTED
event) if the spawn would overflow with OverflowAction.REJECT;
otherwise returns silently. Prevents partial multi-input commits where one
template's spawn lands in its slot queue while another pauses for operator."""


async def _methods_to_generator(methods: List[ExecutingMethod]) -> AsyncGenerator[ExecutingMethod, None]:
    for method in methods:
        yield method

orca_logger = logging.getLogger("orca")


def _recovery_event_for(decision: RecoveryDecision) -> ThreadEvent:
    """The TSM event that un-parks a paused thread for an operator's decision."""
    if decision == RecoveryDecision.RETRY_ACTION:
        return ThreadEvent.RECOVERY_RETRY
    # The aborts and CONTINUE all leave the action behind, so they share one
    # event; the abort raises out of the transient state it lands in.
    if decision in (
        RecoveryDecision.CONTINUE,
        RecoveryDecision.ABORT_ACTION,
        RecoveryDecision.ABORT_METHOD,
        RecoveryDecision.ABORT_THREAD,
    ):
        return ThreadEvent.RECOVERY_SKIP
    raise ValueError(
        f"RecoveryDecision.{decision.name} has no thread event. RETRY is settled "
        "at the operation seam and never reaches action-level recovery; any new "
        "decision must be mapped here rather than defaulting to leaving the action "
        "behind."
    )


_ABORTS_THAT_END_AN_UNBOUND_THREAD = frozenset({
    RecoveryDecision.ABORT_ACTION,
    RecoveryDecision.ABORT_METHOD,
})


class _ThreadAbortedSignal(Exception):
    """Internal sentinel raised when a thread receives RecoveryDecision.ABORT_THREAD.

    Distinguishes operator-initiated thread abort from a genuine action
    failure (which propagates as the original exception and lands the
    execution at FAILED). The thread loop catches this in
    :meth:`ExecutingLabwareThread.start`, transitions the thread to
    ``LabwareThreadStatus.ABORTED``, and returns cleanly so the workflow
    sees a terminal-but-non-failure thread state.
    """


class _ThreadStopSignal(Exception):
    """Internal sentinel raised when ``stop_event`` wins the
    AWAITING_CO_THREADS race. Lets ``start()`` bypass the rest of the
    loop + ``_handle_thread_completion`` (which would try to move to
    end-location) and go straight to ``_handle_thread_stop`` so the
    thread lands at ``STOPPED`` immediately.
    """


class _SharedActionPausedError(Exception):
    """The sympathetic ``last_error`` a contributor carries while it is PAUSED as
    part of an action group whose owner failed the shared action. Names the shared
    step on the contributor's snapshot; the owner records the one real incident, so
    this never becomes an incident of its own."""

    def __init__(self, action_command: str) -> None:
        super().__init__(
            f"paused: shared action '{action_command}' failed on the owner"
        )
        self.action_command = action_command


async def _settled(outcome: asyncio.Future[ActionResolution]) -> bool:
    """True once ``outcome`` is done; cancelling this wait leaves ``outcome`` untouched."""
    await asyncio.wait({outcome})
    return True


class ExecutingLabwareThread(ILabwareThread, IThreadContext):

    # Class-level default so test code that bypasses __init__ via __new__()
    # observes None rather than AttributeError. Real instances shadow this
    # via __init__'s instance assignment.
    _capacity_precheck_callback: CapacityPrecheckCallback | None = None
    _status_recorder: IThreadStatusRecorder | None = None

    def __init__(self,
                 thread: LabwareThreadInstance,
                 event_bus: IEventBus,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                 actions_resolver: DynamicResourceActionResolver,
                 context: WorkflowExecutionContext,
                 labware_location_service: ILabwareLocationService,
                 coordination_config: CoordinationConfig | None = None,
                 thread_incident_declarer: IThreadIncidentDeclarer | None = None,
                 method_registry: IMethodRegistry | None = None,
                 executing_method_registry: ExecutingMethodRegistry | None = None,
                 variable_store: IVariableResolver | None = None,
                 register_method_template: Callable[[str, MethodTemplate], None] | None = None,
                 command_pipeline: SystemCommandPipeline | None = None,
                 residency_check: ResidencyCheck | None = None,
                 status_recorder: IThreadStatusRecorder | None = None,
                 ) -> None:
        self._thread = thread
        self._status_recorder = status_recorder
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._state_machine = ThreadStateMachine(initial=LabwareThreadStatus.CREATED)
        self._coordination_config = coordination_config or CoordinationConfig()
        self._context: WorkflowExecutionContext = context
        self._event_bus = event_bus
        self._action_resolver = actions_resolver
        # When set (by ExecutingThreadFactory, populated by
        # SystemRuntime.__init__), the typed UnresolvableDeadlockError catch
        # below auto-records IncidentCategory.UNRESOLVABLE_DEADLOCK and fans
        # out pause_all_threads so the deadlock surfaces on every operator
        # surface (orca incidents list / GET /api/incidents). Optional so tests that build threads directly stay simple.
        self._thread_incident_declarer: IThreadIncidentDeclarer | None = thread_incident_declarer
        # Yield thread: MergeLane created lazily in start() after registry is set
        self._method_lane: MergeLane[ExecutingMethod] = MergeLane(
            _methods_to_generator([]),  # placeholder, replaced in start()
            name_getter=lambda m: m.name,
        )
        self._assigned_method: ExecutingMethod | None = None
        self._bound_to_bring_labware_to: IMethod | None = None

        self._completed_methods: List[ExecutingMethod] = []
        self._labware_location_service = labware_location_service
        self._command_pipeline = command_pipeline


        # A bind already claimed the slot and its arrival wrote the ledger; everything else is expected until it arrives.
        if self._thread.start_location.labware is not self._thread.labware:
            self._require_pipeline().placer.expected(
                self._thread.labware,
                self._thread.start_location,
                arrival_mechanism_for(self._thread),
            )
        self._location_history = labware_location_service.get_history(self._thread.labware)
        self._holdover = ReservationHoldover(residency_check)
        self._assigned_action: ExecutableLocationAction | None = None
        self._move_action: MoveAction | None = None
        self._labware_moved_event = asyncio.Event()
        self._labware_discharged = False
        self._planned_from: Location | None = None
        self._following_peer_action = False
        self._stopped_before_the_owners_abort = False
        self._lock_wait = LockWait(owner=self._thread.name)
        self._stop_event = asyncio.Event()
        # Bind the cooperative-stop event onto the value-object so LIVE-
        # mode spawn strategies can poll it during their operator-wait
        # loops. Without this seam `thread.stop()` would only take
        # effect after the operator-wait returned (review item L1).
        self._thread.set_stop_event(self._stop_event)
        self.completed = asyncio.Event()
        self._manual_start = False
        self._auto_spawn_callback: AutoSpawnCallback | None = None
        self._capacity_precheck_callback: CapacityPrecheckCallback | None = None
        self._work_finished_hook: Callable[..., None] | None = None
        self._auto_spawned: set[tuple[str, str]] = set()
        self._partner_constraints: dict[str, dict[str, str]] = {
            template_name: {"barcode": barcode} for template_name, barcode in thread.named_partners.items()
        }

        # Park/wake lifecycle
        self._labware_registry: ILabwareRegistry | None = None
        # True when this receiver's generator returned because its labware was
        # used up. Spent labware must not stay on its slot for the next
        # receiver to adopt, whatever the template's end disposition says.
        self._ended_spent: bool = False
        self._stopped_taking_contributions: bool = False
        self._handed_the_slot_on: bool = False

        # Error recovery pause/resume
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # not paused initially
        self._recovery_decision: RecoveryDecision | None = None
        # The group whose one decision this error pause waits on, fixed when the pause begins.
        self._decided_by_group: SharedActionCoordination | None = None
        self._last_error: Exception | None = None
        # What the thread was doing when it stopped, in words. The exception
        # alone cannot say: "pf400_1 is not connected" fits a home or a pick.
        self._pause_message: str | None = None
        self._pause_site: PauseSite | None = None
        # The device command this thread is paused inside; None at every other
        # pause. Names the call RETRY re-runs.
        self._op_pause_command: str | None = None

        # Manual pause (cooperative). The event makes the request
        # awaitable so the AWAITING_CO_THREADS wait loop can race it
        # alongside ``all_labware_is_present`` / ``exit_signal``; boundary
        # checks at the method/action seams use ``is_set()`` exactly as
        # they used the previous bool flag.
        self._pause_request_event: asyncio.Event = asyncio.Event()
        self._held_at_start = False
        self._start_labware_never_arrived = False
        self._reached_start = False
        self._failed_under_abort_policy = False
        # Who asked for a non-error pause: "manual" (a person) or "system"
        # (the runtime protecting itself). Overwritten on every request.
        self._pause_reason: str = "manual"

        # Cross-thread error propagation
        self._event_channel_registry: EventChannelRegistry | None = None

        # Candidate locations the reservation retry loop is
        # racing against. Populated by ``notify_awaiting_reservation`` once
        # the first reservation cycle fails to grant; ``derive_waiting_for``
        # reads it for the ``AWAITING_ACTION_RESERVATION`` snapshot. Cleared
        # on grant / typed timeout via ``clear_awaiting_reservation``.
        self._pending_reservation_candidates: List[str] = []

        # Factory-level state surfaced via IThreadContext so
        # IMethodTemplate.schedule() implementations can instantiate
        # methods without reaching back into ExecutingThreadFactory.
        self._method_registry: IMethodRegistry | None = method_registry
        self._executing_method_registry_field: ExecutingMethodRegistry | None = (
            executing_method_registry
        )
        self._variable_store: IVariableResolver = variable_store or NullVariableResolver()
        self._register_method_template: Callable[[str, MethodTemplate], None] | None = (
            register_method_template
        )

    def publish_initial_status(self) -> None:
        """Broadcast initial ``CREATED`` to StatusManager. Kept off ``__init__``
        so unit tests that pass mock contexts don't hit Pydantic validation."""
        self._publish_status_to_status_manager(self._state_machine.current)

    def notify_awaiting_reservation(self, candidate_position_ids: List[str]) -> None:
        """``IActionReservationStatusSink`` impl: park visibly on reservation.

        Sets status to ``AWAITING_ACTION_RESERVATION`` (which emits via the
        StatusManager) and stores the candidate list for snapshot
        enrichment. Called once per retry cycle from
        ``ResourcePoolResolver.resolve_action_location`` after each
        reject/deadlock outcome, so the dashboard sees ticks instead of a
        silent stall.
        """
        self._pending_reservation_candidates = list(candidate_position_ids)
        self._fire(ThreadEvent.RESERVATION_AWAITED)

    def clear_awaiting_reservation(self) -> None:
        """``IActionReservationStatusSink`` impl: reservation acquired (or aborted).

        Wipes the candidate list and drops back to
        ``RESOLVING_ACTION_LOCATION`` so the move-or-execute layer can
        flip status forward without leaving stale ``waiting_for`` data
        on the snapshot. The reservation-retry loop wraps its body in
        try/finally so this fires on grant AND on raise.
        """
        if not self._pending_reservation_candidates:
            return
        self._pending_reservation_candidates = []
        if self.status == LabwareThreadStatus.AWAITING_ACTION_RESERVATION:
            self._fire(ThreadEvent.RESERVATION_GRANTED)

    async def _wait_for_the_owner_to_bind(self) -> ExecutableLocationAction:
        """A contributor's wait for its owner's action; it pauses where it waits when the run pauses."""
        assert self._assigned_method is not None
        while True:
            bound = asyncio.ensure_future(self._assigned_method.wait_for_current_action())
            pause = asyncio.ensure_future(self._pause_request_event.wait())
            try:
                await asyncio.wait([bound, pause], return_when=asyncio.FIRST_COMPLETED)
                if bound.done():
                    return bound.result()
            finally:
                await drain_cancelled_waiters(bound, pause)
            await self.hold_for_pause()

    async def hold_for_pause(self) -> None:
        """``IActionReservationStatusSink`` impl: pause while waiting for a device; a stop ends the thread."""
        await self._handle_manual_pause()
        if self._stop_event.is_set():
            raise _ThreadStopSignal()

    @property
    def pending_reservation_candidates(self) -> List[str]:
        """Read-only view of the candidate locations the thread is waiting on.

        Empty unless the thread is parked in ``AWAITING_ACTION_RESERVATION``
        or carrying its labware to its start. ``derive_waiting_for`` reads
        this directly; tests and snapshot consumers should treat the
        return as immutable.
        """
        return list(self._pending_reservation_candidates)

    def set_event_channel_registry(self, registry: EventChannelRegistry) -> None:
        self._event_channel_registry = registry

    def set_auto_spawn_callback(self, callback: AutoSpawnCallback) -> None:
        self._auto_spawn_callback = callback

    def set_capacity_precheck_callback(self, callback: CapacityPrecheckCallback) -> None:
        self._capacity_precheck_callback = callback

    def set_work_finished_hook(self, hook: Callable[..., None]) -> None:
        """Register a callback fired when this thread stops being able to
        contribute: any terminal state, or the park waiting to be collected.

        Signature expected: ``hook(thread: ExecutingLabwareThread) -> None``.
        Precise self-reference is elided from the annotation to avoid
        forward-reference quoting; the workflow is the sole caller and
        invokes with the correct type.
        """
        self._work_finished_hook = hook

    def _my_slot_key(self) -> str:
        """Compose the slot key for THIS thread's receiver slot.

        Mirrors the auto-spawn callback's slot_key_for(template, ctx) call so
        the receiver, contributor, and handoff paths all address the same
        LabwareSlot under group-aware keying. Falls back to labware_name when
        the thread isn't group/submission-tagged.
        """
        assert self._labware_registry is not None
        tpl = self._thread.thread_template
        if tpl is None:
            return (
                self._thread.labware_template.name
                if self._thread.labware_template is not None
                else self._thread.name
            )
        ctx = None
        if self._thread.submission_id is not None:
            ctx = GroupExecutionContext(
                group_id=self._thread.group_id,
                submission_id=self._thread.submission_id,
                batch_mode=self._thread.batch_mode,
            )
        return self._labware_registry.slot_key_for(tpl, ctx, self._thread.named_barcode)

    @property
    def id(self) -> str:
        return self._thread.id

    @property
    def name(self) -> str:
        return self._thread.name

    @property
    def start_location(self) -> Location:
        return self._thread.start_location

    @property
    def end_locations(self) -> list[Location]:
        return self._thread.end_locations

    @end_locations.setter
    def end_locations(self, locations: list[Location]) -> None:
        self._thread.end_locations = locations

    @property
    def labware(self) -> LabwareInstance:
        return self._thread.labware

    @property
    def has_reached_its_start(self) -> bool:
        return self._reached_start

    def append_method_sequence(self, method: IMethod) -> None:
        self._thread.append_method_sequence(method)

    @property
    def location_history_names(self) -> List[str]:
        return self._location_history.get_history_names()

    @property
    def method_lane(self) -> MergeLane[ExecutingMethod]:
        return self._method_lane

    @property
    def completed_methods(self) -> List[ExecutingMethod]:
        return self._completed_methods

    @property
    def assigned_method(self) -> ExecutingMethod | None:
        return self._assigned_method

    @property
    def assigned_action(self) -> ExecutableLocationAction | None:
        return self._assigned_action

    @property
    def move_action(self) -> MoveAction | None:
        return self._move_action

    @property
    def following_peer_action(self) -> bool:
        """True while this thread is a contributor awaiting the owner's outcome.

        The status says ``EXECUTING_ACTION`` because the shared action is the one
        running, but this thread drives nothing and holds no reservation to
        release, so anything reasoning about progress must ask here as well.
        """
        return self._following_peer_action

    @property
    def blocked_on_lock(self) -> str | None:
        """The device or transporter lock this thread is queued for, or None.

        A lock wait wears the status of whatever the thread was already doing,
        so a mover stuck behind a long driver call reads ``MOVING`` with no
        subject unless the answer comes from here.
        """
        return self._lock_wait.waiting_on

    def _require_pipeline(self) -> SystemCommandPipeline:
        """A move runs and records through the pipeline, so a thread that can move has to have one."""
        if self._command_pipeline is None:
            raise RuntimeError(
                f"thread {self._thread.name!r} has no command pipeline, so a move "
                f"has nowhere to record what it did"
            )
        return self._command_pipeline

    @property
    def current_location(self) -> Location:
        """Get current location from ILabwareLocationService (single source of truth)."""
        return self._labware_location_service.get(self._thread.labware)

    @property
    def context(self) -> WorkflowExecutionContext:
        if self._context is None:
            raise ValueError("Context is not set. Call start() before accessing context.")
        return self._context

    @property
    def status(self) -> LabwareThreadStatus:
        status = self._status_manager.get_status(self._thread.id)
        return LabwareThreadStatus[status]

    def _fire(self, event: ThreadEvent) -> None:
        """Single entry point for every status mutation on this thread."""
        new_status = self._state_machine.transition(event)
        self._publish_status_to_status_manager(new_status)

    async def _take_labware_already_on_the_deck(self) -> bool:
        """Get labware that is already on the deck to the start. False when it is not on the deck,
        so the spawn brings it in."""
        current = self._labware_location_service.present_location(self._thread.labware)
        if current is None:
            return False
        start = self._thread.start_location
        decision = decide_start(StartFacts(
            present=True,
            at_start=current.position_id == start.position_id,
            reachable=self._move_handler.has_route(current, start),
        ))
        if decision is StartDecision.MOVE:
            self._pending_reservation_candidates = [start.name]
            try:
                await self._fire_and_execute_move_to(ThreadEvent.MOVE_RESERVATION_REQUESTED, [start])
            finally:
                self._pending_reservation_candidates = []
        elif decision is StartDecision.ASK_OPERATOR:
            await self._have_the_operator_place_it(current, start)
        return True

    async def _have_the_operator_place_it(self, current: Location, start: Location) -> None:
        spawn = RelocateByHandSpawn(
            start, current, self._labware_location_service, self._require_pipeline().placer, self._move_handler,
            SpawnWaitHooks(
                blocked=lambda: self._fire_from(LabwareThreadStatus.CREATED, ThreadEvent.START_LOCATION_AWAITED),
                cleared=lambda: self._fire_from(
                    LabwareThreadStatus.AWAITING_START_LOCATION, ThreadEvent.START_LOCATION_CLEARED,
                ),
            ),
        )
        if self._thread.run_mode is WorkflowRunMode.LIVE:
            self._fire(ThreadEvent.LIVE_MANUAL_PLACE_AWAITED)
        await spawn.acquire(self._thread)

    def _fire_from(self, status: LabwareThreadStatus, event: ThreadEvent) -> None:
        """Fire ``event`` only while the thread is in ``status``; a pause or stop may have moved it on."""
        if self._state_machine.current is status:
            self._fire(event)

    def _manual_intervention_location(
        self, status: LabwareThreadStatus,
    ) -> str | None:
        """Target slot name for a manual place/remove park, else None.

        Mirrors `derive_waiting_for`: a LIVE manual place waits on the
        start_location, a manual remove on the slot the labware rests at
        (its granted end candidate).
        """
        if status == LabwareThreadStatus.AWAITING_MANUAL_PLACE:
            return self._thread.start_location.name
        if status == LabwareThreadStatus.AWAITING_MANUAL_REMOVE:
            return self.current_location.name
        return None

    def _publish_status_to_status_manager(self, status: LabwareThreadStatus) -> None:
        pause_reason: str | None = None
        last_error: str | None = None
        if status == LabwareThreadStatus.PAUSED:
            pause_reason = "error" if self._last_error is not None else self._pause_reason
        # Terminal FAILED/ABORTED carry their cause too: the run record
        # upserts every transition, and a None here would erase the stored WHY.
        if self._last_error is not None and status in (
            LabwareThreadStatus.PAUSED,
            LabwareThreadStatus.FAILED,
            LabwareThreadStatus.ABORTED,
        ):
            last_error = str(self._last_error)
        context: ThreadExecutionContext
        manual_location = self._manual_intervention_location(status)
        changed = not self._status_manager.has_status(self._thread.id, status.name)
        if changed and self._status_recorder is not None:
            self._status_recorder.record_thread_status(
                self._context.execution_id,
                thread_id=self._thread.id,
                name=self._thread.name,
                template_name=self._thread.template_name,
                status=status.name,
                last_error=last_error,
                pause_reason=pause_reason,
                group_id=self._thread.group_id,
            )
        if manual_location is not None:
            context = ManualInterventionContext(
                execution_id=self._context.execution_id,
                workflow_name=self._context.workflow_name,
                thread_id=self._thread.id,
                thread_name=self._thread.name,
                template_name=self._thread.template_name,
                group_id=self._thread.group_id,
                pause_reason=pause_reason,
                last_error=last_error,
                labware_id=self._thread.labware.id,
                labware_name=self._thread.labware.name,
                labware_template_name=(
                    self._thread.labware_template.name
                    if self._thread.labware_template is not None
                    else None
                ),
                target_location=manual_location,
            )
        else:
            context = ThreadExecutionContext(
                execution_id=self._context.execution_id,
                workflow_name=self._context.workflow_name,
                thread_id=self._thread.id,
                thread_name=self._thread.name,
                template_name=self._thread.template_name,
                group_id=self._thread.group_id,
                pause_reason=pause_reason,
                last_error=last_error,
            )
        self._status_manager.set_status("THREAD", self._thread.id, status.name, context)
        # Terminal states: fire ``completed`` so anyone awaiting the thread
        # task unblocks regardless of whether the exit was natural completion,
        # operator stop, or operator-initiated abort.
        if status.is_terminal():
            self.completed.set()
        # Work-finished hook: releases this thread from the feeder pool for
        # slot-close evaluation. It reads the same predicate the sweep does,
        # so the close can never become due at a status the hook stays quiet
        # for. PAUSED does not qualify: it resumes.
        if self._work_finished_hook is not None and self.has_finished_its_work():
            self._work_finished_hook(self)

    @property
    def manual_start(self) -> bool:
        """Whether this thread requires manual start by event handler."""
        return self._manual_start

    @manual_start.setter
    def manual_start(self, value: bool) -> None:
        """Set whether this thread requires manual start by event handler."""
        self._manual_start = value

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    @property
    def pause_reason_hint(self) -> str:
        """Who last asked this thread to pause: "manual" or "system".

        Consulted only when the thread is PAUSED and not error-paused
        (an error pause always reports "error", regardless of this).
        """
        return self._pause_reason

    @property
    def pause_message(self) -> str | None:
        """What the thread was doing when it stopped, in words: which seam
        raised for an error pause, or the system's cause for a cooperative
        one. None when neither supplied one."""
        return self._pause_message

    @property
    def pause_site(self) -> PauseSite | None:
        """WHERE this thread stopped, or None when it is not error-paused.

        Different sites honour different recovery decisions, so this is what an
        operator surface reads before offering any.

        The group's site, not this thread's stamp, while it is following a
        peer's action. A contributor stamps its own once, when it first sees the
        group paused, and the group moves under it: the owner can fail inside a
        device call, take a RETRY, and fail again in the action body around it.
        The stamp would still say the device call and offer RETRY for one
        that is no longer suspended. Same reasoning as
        ``paused_device_command``, which has always read the group.
        """
        group = self._shared_action_group()
        if self._following_peer_action and group is not None and group.action_paused.is_set():
            return (
                group.step_paused_site or PauseSite.ACTION_BODY
            )
        return self._pause_site

    @property
    def honoured_decisions(self) -> list[RecoveryDecision]:
        """The recovery decisions this thread will accept right now.

        Empty when it is not error-paused. Sending anything not in here is
        refused and the thread stays paused, so a surface can offer exactly
        these and nothing else. At a failed move CONTINUE additionally needs
        the ledger to put the labware at the target, which the operator records
        and this cannot know in advance.
        """
        site = self.pause_site
        if site is None or not self.is_error_paused:
            return []
        return sorted(HONOURED_DECISIONS[site].honours, key=ESCALATION_ORDER.index)

    @property
    def paused_device_command(self) -> str | None:
        """The device call this thread is suspended inside, or None.

        The group's answer when this thread is part of a paused action group: a
        contributor never drives the call, so its own record is empty while the
        group is suspended in one. This is what RETRY at a device call is judged against, so
        an operator surface offering that verb reads the same answer the
        runtime will accept it on.
        """
        group = self._shared_action_group()
        if group is not None and group.action_paused.is_set():
            return group.op_paused_command
        return self._op_pause_command

    @property
    def is_error_paused(self) -> bool:
        return self.status == LabwareThreadStatus.PAUSED and self._last_error is not None

    @property
    def is_manual_paused(self) -> bool:
        return self.status == LabwareThreadStatus.PAUSED and self._last_error is None

    def hold_at_start(self, reason: str = "manual") -> None:
        """Freeze this thread before it acquires its start labware.

        For a thread started into a paused execution: it missed the pause
        fan-out, and the ordinary first boundary is too late. By then it has
        taken its start plate (a stacker dispense, or an operator prompted to
        hand-load one) and run a whole action, move included, on a stopped
        run. Only threads held this way stop at that early point; an operator
        pausing a thread that has not started yet still lands on the first
        method boundary, where there is a method to act on.

        ``reason`` mirrors ``request_pause``'s: a thread born into a
        system-triggered hold must not report "manual" just because it
        never reached a running checkpoint to be told otherwise.
        """
        self._held_at_start = True
        self._pause_reason = reason
        self._pause_request_event.set()

    @property
    def pause_requested(self) -> asyncio.Event:
        return self._pause_request_event

    def request_pause(self, reason: str = "manual", message: str | None = None) -> None:
        """Request cooperative pause at the next safe point.

        Boundary checkpoints (between methods, between actions) read
        ``_pause_request_event.is_set()``. The AWAITING_CO_THREADS wait
        loop races the event so a thread parked on a co-labware wait
        also honors the request without waiting for its peer to
        arrive.

        ``reason`` is reported back as ``pause_reason`` once the thread
        actually lands in PAUSED, unless it is error-paused (that reports
        "error" regardless of ``reason`` here). ``message`` is likewise
        reported back as ``pause_message`` -- the cause a system-triggered
        pause already has in hand (a stall report, an incident) instead of
        leaving the operator to guess from "system" alone. Both are
        overwritten on every request, cleared to None when a manual pause
        supplies no message, so a later plain pause cannot inherit a
        stale one.
        """
        self._pause_reason = reason
        self._pause_message = message
        self._pause_request_event.set()

    def cancel_pending_pause(self) -> bool:
        """Clear a pending pause request that has not yet fired.

        Returns ``True`` iff a pause was queued and is now cancelled.
        Callers in ``resume_all_threads`` only invoke this on
        non-PAUSED threads (PAUSED threads go through
        ``resume_from_manual_pause`` instead), so the return value
        directly mirrors ``_pause_request_event.is_set()`` at call
        time.

        The contract is "cooperatively cancel the next-safe-point
        pause." Threads currently MOVING / EXECUTING_ACTION have
        not reached a pause checkpoint; clearing the event means
        they continue past the next checkpoint instead of latching
        into PAUSED. AWAITING_CO_THREADS USED to share this fate;
        post-fix the wait loop races the event hot, so a cancel
        between ``request_pause`` and the loop's next pass-through
        cleanly aborts the pause.
        """
        had_pending = self._pause_request_event.is_set()
        self._pause_request_event.clear()
        return had_pending

    def resume_from_manual_pause(self) -> None:
        """Resume a manually paused thread. Raises if paused from error."""
        if self.status != LabwareThreadStatus.PAUSED:
            raise ValueError(f"Thread {self.name} is not paused (status: {self.status.name})")
        if self._last_error is not None:
            raise ValueError(
                f"Thread {self.name} is paused due to error, use recover_thread with a decision"
            )
        self._pause_request_event.clear()
        self._resume_event.set()

    def resume_with_decision(self, decision: RecoveryDecision) -> None:
        """Resume an error-paused thread with the given recovery decision.

        Manual-pause paths use `resume_from_manual_pause`; calling this method
        on a manually paused thread would leave the decision dangling and
        silently no-op (the manual-pause handler does not invoke
        `handle_recovery`), so refuse loudly instead.
        """
        if self.status != LabwareThreadStatus.PAUSED:
            raise ValueError(f"Thread {self.name} is not paused (status: {self.status.name})")
        if self._last_error is None:
            raise ValueError(
                f"Thread {self.name} is manually paused, not error-paused. "
                f"Resume it instead of recovering it."
            )
        self._refuse_a_verb_this_site_does_not_honour(decision)
        if decision == RecoveryDecision.CONTINUE:
            self._refuse_continue_the_ledger_does_not_back()
        self._deliver_decision(decision)

    def _deliver_decision(self, decision: RecoveryDecision) -> None:
        # The channel the pause is waiting on, as it was fixed when the pause began: a contributor
        # leaving the group mid-pause must not move the decision onto a channel nobody waits on.
        group = self._decided_by_group or self._group_deciding(self.pause_site)
        if group is not None:
            group.submit_decision(decision)
            return
        self._recovery_decision = decision
        self._resume_event.set()

    def _refuse_a_verb_this_site_does_not_honour(
        self, decision: RecoveryDecision,
    ) -> None:
        """Raise unless the site this thread stopped at honours the verb.

        The thread stays paused, so the operator picks again. Before this, an
        unhonoured verb reached the site and fell through: at most sites into
        code that failed the thread, and at resolution and deadlock into a
        silent ABORT_THREAD. Neither is what the operator asked for, and the
        first destroys work the second at least ends cleanly.
        """
        site = self.pause_site
        if site is None:
            raise ValueError(
                f"Thread {self.name} is paused but does not say where, so no "
                f"decision can be judged. This is an engine bug; report it."
            )
        recovery = HONOURED_DECISIONS[site]
        if decision in recovery.honours:
            return
        honoured = ", ".join(
            d.name for d in sorted(recovery.honours, key=ESCALATION_ORDER.index)
        )
        # RETRY_ACTION is refused everywhere but ACTION_BODY, and most sites'
        # own reasons are about other verbs, which would not answer the operator.
        because = (
            "it re-runs an action whose own Python failed, and that is not "
            "what failed here"
            if decision is RecoveryDecision.RETRY_ACTION
            and site not in (PauseSite.DEVICE_OP, PauseSite.COMMAND_SAVE)
            else recovery.because
        )
        raise ValueError(
            f"Thread {self.name} stopped at {site.value} and does not honour "
            f"{decision.name} there, because {because}. It honours "
            f"{honoured}. The thread is still paused; send one of those."
        )

    def _refuse_continue_the_ledger_does_not_back(self) -> None:
        """Raise when CONTINUE at a failed move is not what the ledger says.

        Legality and evidence are different questions. The site table says
        CONTINUE applies at a move; this says whether the operator has actually
        done the thing it claims. CONTINUE there means they carried the labware
        to the target themselves, which only holds once the ledger says the
        labware IS at the target.
        """
        move = self._move_action
        if self.pause_site is not PauseSite.MOVE or move is None:
            return
        recorded = self._ledger_position(move.labware)
        if recorded is not None and recorded.position_id == move.target.position_id:
            return
        raise ValueError(
            f"Thread {self.name} is paused on a failed move of "
            f"'{move.labware.name}' to '{move.target.position_id}', and the "
            f"ledger has it at "
            f"'{recorded.position_id if recorded else 'nowhere recorded'}'. "
            f"CONTINUE means the move is done: put the labware at the target, "
            f"record that with labware edit-location, then CONTINUE. If it is "
            f"anywhere else, record that instead and RETRY -- the move is "
            f"planned again from wherever you say it is. Or ABORT_THREAD."
        )

    def _ledger_position(self, labware: LabwareInstance) -> Location | None:
        """Where the ledger says the labware IS, or None if it is not there yet.

        Callers compare by ``position_id``: the location service treats two
        Locations sharing one position id as the same place, so identity would
        refuse a labware that is correctly recorded at the target.
        """
        try:
            if self._labware_location_service.placement(labware) is not PlacementState.PRESENT:
                return None
            return self._labware_location_service.get(labware)
        except KeyError:
            return None

    @property
    def template_name(self) -> str:
        return self._thread.template_name

    @property
    def labware_template(self) -> LabwareTemplate | None:
        return self._thread.labware_template

    @property
    def thread_instance(self) -> LabwareThreadInstance:
        return self._thread

    def has_completed(self) -> bool:
        # ABORTED + STOPPED count as "thread is done with its lifecycle" so
        # ``ThreadManager.has_completed()`` returns True after operator
        # aborts, matching the intent of "all threads have finished" rather
        # than "all threads ran their full method sequence".
        return self.status.is_terminal()

    def has_finished_its_work(self) -> bool:
        """True once this thread will neither feed a receiver nor ask for a
        slot again.

        Every terminal state, plus the park where the labware has arrived at
        its end location and the thread is only waiting for an operator to
        pick it up. That park sits after the last action and the end move, so
        the thread's contributions are all made. It is NOT terminal, and must
        not be: the labware is still physically on the deck.
        """
        return (
            self.has_completed()
            or self.status is LabwareThreadStatus.AWAITING_MANUAL_REMOVE
        )

    def set_partner_constraint(self, template_name: str, constraints: dict[str, str]) -> None:
        self._partner_constraints[template_name] = constraints

    async def _name_my_partners_or_pause(self) -> None:
        """Name this thread's partners on the method it just took. RETRY checks again; it never skips the method."""
        while True:
            try:
                self._name_my_partners_on(self._assigned_method)
                return
            except WrongPartnerPlateError as conflict:
                decision = await self._pause_for_error(
                    conflict, f"thread step failed: {type(conflict).__name__}: {conflict}",
                    PauseSite.THREAD_STEP,
                )
                if decision == RecoveryDecision.RETRY:
                    continue
                self._check_abort_thread(decision, conflict)
                raise conflict

    def _name_my_partners_on(self, method: ExecutingMethod | None) -> None:
        """Owner or contributor, what this thread named binds every method it takes part in."""
        if method is None:
            return
        for template_name, constraints in self._partner_constraints.items():
            method.set_partner_constraints(template_name, constraints)

    def get_partner_constraint(self, template_name: str) -> dict[str, str] | None:
        return self._partner_constraints.get(template_name)

    def set_labware_registry(self, labware_registry: ILabwareRegistry) -> None:
        """Register this thread in the per-execution labware registry.

        The register call is load-bearing: without it the thread does not
        appear in `_labware_registry` for capacity / slot lookups.
        """
        self._labware_registry = labware_registry
        template_name = (
            self._thread.labware_template.name
            if self._thread.labware_template
            else self._thread.name
        )
        labware_registry.register(
            self._thread.labware, template_name, LabwareState.IN_JOURNEY, thread=self,
        )

    async def initialize_labware(self) -> None:
        """Acquire the labware at the thread's start_location via the
        spawn-action strategy.

        Dispatch reads one flag on the thread template:
          - `start_dispense` -> `DispenseSpawn`: physically advances an
            IPlateSource queue via `dispense()`, then writes the slot.
          - anything else -> `ManualPlaceSpawn`: sim modes write the slot
            immediately; LIVE waits for the operator to place the labware
            this thread is already holding. A hand-placed start is the
            default, so the bare string and the explicit MANUAL_PLACE
            spelling both arrive here by falling through.

        Status emission: LIVE-mode ManualPlaceSpawn parks the thread at
        `AWAITING_MANUAL_PLACE` for the duration of the operator wait.
        `derive_waiting_for` surfaces the start_location name so the
        ThreadSnapshot's `waiting_for` field tells the operator which
        slot is being waited on.

        The thread's labware never changes identity here. `labware_register`
        adopts the expectation the ctor recorded rather than minting a second
        instance, so a wait that ends in an arrival leaves every store that
        already knows this thread correct, and a wait that ends any other way
        leaves only the expectation to drop.
        """
        if await self._take_labware_already_on_the_deck():
            return
        spawn = select_spawn_action(
            self._thread, self._labware_location_service, self._require_pipeline().placer, self._move_handler,
            SpawnWaitHooks(
                blocked=lambda: self._fire_from(LabwareThreadStatus.CREATED, ThreadEvent.START_LOCATION_AWAITED),
                cleared=lambda: self._fire_from(
                    LabwareThreadStatus.AWAITING_START_LOCATION, ThreadEvent.START_LOCATION_CLEARED,
                ),
            ),
        )
        if (
            isinstance(spawn, ManualPlaceSpawn)
            and self._thread.run_mode is WorkflowRunMode.LIVE
        ):
            self._fire(ThreadEvent.LIVE_MANUAL_PLACE_AWAITED)
        try:
            await self._acquire_start_labware(spawn)
        except BaseException:
            self._give_up_on_the_start_labware()
            raise
        arrived = (
            self._labware_location_service.placement(self._thread.labware)
            is PlacementState.PRESENT
        )
        if not arrived and self._stop_event.is_set():
            # Cooperatively stopped mid-wait. Nothing physical exists to
            # project, and the expectation must not outlive the thread.
            self._give_up_on_the_start_labware()
            return
        # The arrival told the device: a spawn's placement and an operator's register both go through the placer.
        self._assert_arrived(spawn)

    def _give_up_on_the_start_labware(self) -> None:
        self._require_pipeline().placer.no_longer_expected(self._thread.labware)
        self._start_labware_never_arrived = True

    @property
    def start_labware_never_arrived(self) -> bool:
        """True once the thread ended its start with no labware: the record holds no position for it."""
        return self._start_labware_never_arrived

    async def _acquire_start_labware(self, spawn: SpawnAction) -> None:
        """Run the spawn. A plate source that is offline, or whose dispense fails, pauses the thread: retry, or abort the thread."""
        while True:
            try:
                await spawn.acquire(self._thread)
                return
            except (DeviceOfflineError, PlateDispenseError) as offline:
                # Only the dispense: once the slot holds the plate, a second dispense would jam the output.
                dispense_failed = self._thread.start_location.labware is not self._thread.labware
                if not (isinstance(spawn, DispenseSpawn) and dispense_failed):
                    raise
                try:
                    decision = await self._pause_for_error(
                        offline, f"start dispense failed: {type(offline).__name__}: {offline}",
                        PauseSite.THREAD_STEP,
                    )
                except asyncio.CancelledError:
                    self._fire_thread_stopped_on_cancel()
                    raise
                if decision == RecoveryDecision.RETRY:
                    self._fire(ThreadEvent.RECOVERY_RETRY)
                    continue
                self._check_abort_thread(decision, offline)
                raise

    def _assert_arrived(self, spawn: SpawnAction) -> None:
        """A spawn that returns without a stop must have placed the labware.

        Loud on purpose. Skipping the deck projection instead would let the run
        continue with the driver deck never told about this plate, and the
        failure would surface later as an unexplained pick, far from the spawn
        that never wrote the slot.
        """
        placement = self._labware_location_service.placement(self._thread.labware)
        if placement is PlacementState.PRESENT:
            return
        raise SpawnDidNotPlaceError(
            thread_name=self._thread.name,
            labware_name=self._thread.labware.name,
            location=self._thread.start_location.position_id,
            spawn=type(spawn).__name__,
            placement=placement.value,
        )

    def stop(self) -> None:
        # Idempotent: shutdown paths (ThreadManager.stop_all_threads,
        # cleanup_parked_threads) iterate every thread unconditionally,
        # so the table-strict _fire(STOP_REQUESTED) must short-circuit
        # on threads already stopping or finalized.
        if self.status is LabwareThreadStatus.STOPPING or self.status.is_terminal():
            return
        if not self._owners_abort_published():
            self._stopped_before_the_owners_abort = True
        # An error pause already under way ends the way a person would end it
        # (a pause that starts after this takes the stop event): the action gives its device back and says
        # what it dropped. A contributor following a peer is excluded -- its own
        # wait already races the stop event, and feeding it a decision would
        # take the one the owner is waiting for.
        if self.is_error_paused and not self._following_peer_action:
            self.resume_with_decision(RecoveryDecision.ABORT_THREAD)
            return
        # Load-bearing: wakes a contributor parked pre-binding in
        # wait_for_current_action, which does not watch stop_event.
        self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_METHOD)
        self._fire(ThreadEvent.STOP_REQUESTED)
        self._stop_event.set()

    def _handle_thread_stop(self) -> None:
        # A thread stopped mid-wait registered its labware before it started,
        # and a stale entry shows up in find_and_claim walks and operator
        # clears. Idempotent, so every stop path can call it.
        if self._labware_registry is not None:
            self._labware_registry.unregister(self._thread.labware.id)
        self._stop_event.clear()
        self._fire(ThreadEvent.STOP_COMPLETE)

    def _is_a_joiner_the_action_does_not_take(self, unresolved: UnresolvedLocationAction) -> bool:
        """A joined plate stays where it is for an action of the shared method that does not take it as an input."""
        assert self._assigned_method is not None and self._thread.labware_template is not None
        if not self._assigned_method.shared_coord.is_contributor(self._thread.id):
            return False
        mine = self._thread.labware_template.name
        return not any(
            isinstance(template, AnyLabwareTemplate) or template.name == mine
            for template in unresolved.expected_input_templates
        )

    def _inputs_with_no_thread(self, unresolved: UnresolvedLocationAction) -> list[LabwareTemplate]:
        """The action's other labware no thread brings yet: what this thread spawns for. A filled input slot
        already has a live thread (entry threads pre-bind their labware)."""
        assert self._thread.labware_template is not None
        mine = self._thread.labware_template.name
        return [
            template for template in unresolved.expected_input_templates
            if not isinstance(template, AnyLabwareTemplate)
            and template.name != mine
            and not unresolved.is_input_assigned(template)
        ]

    async def _auto_spawn_for_action(self, unresolved: UnresolvedLocationAction) -> None:
        if self._auto_spawn_callback is None:
            return
        assert self._assigned_method is not None
        # Contributors (joined via orca.join()) should not auto-spawn.
        # Only the owning thread spawns contributors for its actions.
        if self._assigned_method.shared_coord.is_contributor(self._thread.id):
            return
        candidates = [
            template for template in self._inputs_with_no_thread(unresolved)
            if (unresolved.id, template.name) not in self._auto_spawned
        ]

        # Pre-check pass: for multi-input actions with REJECT
        # policies, raise upfront so no template's spawn lands in its slot
        # queue while a later template pauses for operator. Without this,
        # the queued contribution's receiver would block waiting for the
        # paused template's labware.
        contributor_ctx: GroupExecutionContext | None = None
        if self._thread.submission_id is not None:
            contributor_ctx = GroupExecutionContext(
                group_id=self._thread.group_id,
                submission_id=self._thread.submission_id,
                batch_mode=self._thread.batch_mode,
            )
        if self._capacity_precheck_callback is not None:
            for input_template in candidates:
                # Raises CapacityExceededError (and emits
                # REJECTED) if this spawn would overflow with that
                # policy. Side-effect-free for committing -- no queue puts,
                # no contribution increments.
                await self._capacity_precheck_callback(
                    input_template.name, contributor_ctx, self._assigned_method,
                )

        # Commit pass: all candidates either lack a REJECT policy or have
        # room, so no partial multi-input commit can occur.
        for input_template in candidates:
            spawn_key = (unresolved.id, input_template.name)
            self._auto_spawned.add(spawn_key)
            # Stash this thread's group/submission identity on the shared
            # method so the group-aware registry can compose per-group slot
            # keys. None-safe: if this thread is untagged, the
            # registry falls back to the labware_name-only key.
            if contributor_ctx is not None:
                self._assigned_method.set_contributor_context(contributor_ctx)
            try:
                await self._auto_spawn_callback(
                    input_template.name,
                    self._assigned_method,
                    self._thread.run_mode,
                )
            except Exception:
                # A spawn that did not happen is not recorded as done, so RETRY
                # spawns it again.
                self._auto_spawned.discard(spawn_key)
                raise

    async def _auto_spawn_with_recovery(self, unresolved: UnresolvedLocationAction) -> None:
        """Pause wrapper around _auto_spawn_for_action, for a full slot or a spawn that failed.

        RETRY re-attempts the spawn: it succeeds if capacity freed in the interim
        (``slot.close()`` forcing a fresh receiver) or the cause of a failed spawn
        was fixed. ABORT_THREAD ends the thread at ABORTED.

        The action-level verbs never arrive: the spawn callback fires before
        action resolution, so there is no current action to recover, and
        ``HONOURED_DECISIONS`` refuses them on the call, so the operator learns
        at once that the verb does not apply.
        """
        while True:
            try:
                await self._auto_spawn_for_action(unresolved)
                return
            except (_ThreadStopSignal, _ThreadAbortedSignal, OperationDecisionSignal):
                raise
            except CapacityExceededError as error:
                decision = await self._pause_for_error(
                    error, str(error),
                    PauseSite.SPAWN_CAPACITY,
                )
                if decision == RecoveryDecision.RETRY:
                    self._fire(ThreadEvent.RECOVERY_RETRY)
                    continue
                self._check_abort_thread(decision, error)
                raise error
            except Exception as error:
                self._record_auto_spawn_failure(unresolved, error)
                failed = AutoSpawnFailedError(f"{type(error).__name__}: {error}")
                decision = await self._pause_for_error(failed, str(failed), PauseSite.SPAWN_CAPACITY)
                if decision == RecoveryDecision.RETRY:
                    self._fire(ThreadEvent.RECOVERY_RETRY)
                    continue
                self._check_abort_thread(decision, failed)
                raise failed from error

    def _sim_pacer(self) -> SimPacer | None:
        """The run's pacer for a simulated thread; None for LIVE, which hardware paces."""
        if self._thread_incident_declarer is None or self._context is None:
            return None
        return pacer_for_run_mode(
            self._thread.run_mode,
            self._thread_incident_declarer.sim_pacer_for(self._context.execution_id),
        )

    async def start(self) -> None:
        # Seed the per-task run-mode ContextVar from this thread's stamped
        # run_mode, which Submission stamps on the LabwareThreadInstance.
        # Every device dispatch beneath this coroutine reads from this seed
        # via asyncio.Task context inheritance, so sibling threads under
        # different submissions stay isolated.
        current_run_mode.set(self._thread.run_mode)
        # Seed the recoverable-timeout coordinator the same way: device dispatch
        # beneath this coroutine reads it to park a timed-out call for an
        # operator decision. Always set (None when no declarer) so a reused task
        # or fixture can't inherit a stale coordinator -- None = no timeout.
        recoverable_timeout_coordinator.set(
            self._thread_incident_declarer.recoverable_timeout_coordinator
            if self._thread_incident_declarer is not None
            else None,
        )
        # Seed the operation-recovery seam: a device call that fails inside this
        # thread's action body consults this to retry just that op or abort.
        device_op_recovery_handler.set(self._recover_device_op)
        # Seed the lock-wait slot: a device or transporter lock this thread
        # queues for writes into it, so the thread can say what it is behind
        # while its status still reads MOVING or EXECUTING_ACTION.
        current_lock_wait.set(self._lock_wait)
        # Seed the execution id so remote-driver dispatch can tag outbound
        # commands; the gateway controller reads it to tell a workflow command
        # (engine-bounded) from an ad-hoc one (gateway fail-fast timer).
        if self._context is not None:
            current_execution_id.set(self._context.execution_id)
        current_thread.set(RunningThread(id=self._thread.id, name=self._thread.name))
        current_sim_pacer.set(self._sim_pacer())

        try:
            await self._start_body()
        except Exception as exc:
            if self._a_crash_pauses(exc):
                try:
                    await self._pause_until_the_operator_ends_it(exc)
                except asyncio.CancelledError:
                    self._fire_thread_stopped_on_cancel()
                    raise
                return
            # The original crash is the payload the fatal path records; the
            # guarded terminal fire must never replace it.
            self._fire_thread_failed(exc)
            raise

    def _a_crash_pauses(self, error: Exception) -> bool:
        """An error the thread's own handling did not catch pauses, unless the author chose ABORT or the thread is
        being stopped or aborted."""
        if self.status.is_terminal():
            return False
        return not (
            self._failed_under_abort_policy
            or isinstance(error, (_ThreadStopSignal, _ThreadAbortedSignal))
        )

    async def _pause_until_the_operator_ends_it(self, error: Exception) -> None:
        self._record_where_the_crash_left_the_labware(error)
        await self._pause_for_error(
            error,
            f"the thread stopped on an error its own handling did not catch ({type(error).__name__}: {error}); "
            "the failed step is unknown, so the only choice is ABORT_THREAD. Check the labware and take over by hand.",
            PauseSite.UNHANDLED_ERROR,
        )
        self._release_all_held_reservations()
        self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
        self._fire(ThreadEvent.ABORT_THREAD)

    def _record_where_the_crash_left_the_labware(self, error: Exception) -> None:
        """A THREAD_DIED incident naming the labware and its position, which clearing the deck by hand needs.
        A recording failure must not become a second crash."""
        if self._thread_incident_declarer is None or self._context is None:
            return
        location = self.current_location
        try:
            self._thread_incident_declarer.declare_thread_death(
                self._context.execution_id,
                self._thread.id,
                ThreadDiedContext(
                    thread_name=self._thread.name,
                    labware_name=self._thread.labware.name,
                    labware_id=self._thread.labware.id,
                    last_position_id=location.position_id if location is not None else None,
                    error_type=type(error).__name__,
                    error_message=str(error),
                ),
            )
        except Exception:
            orca_logger.exception("Thread %s - recording the crash failed", self._thread.name)

    def _fire_thread_failed(self, cause: Exception) -> None:
        """Land an unhandled crash in terminal FAILED; no-op if already terminal."""
        if self.status.is_terminal():
            return
        # The FAILED event must name its cause; an error-pause that already
        # recorded one keeps the original.
        if self._last_error is None:
            self._last_error = cause
        try:
            self._fire(ThreadEvent.THREAD_FAILED)
        except Exception:
            orca_logger.exception(
                "Thread %s - THREAD_FAILED fire raised; original crash re-raised",
                self._thread.name,
            )

    def _fire_thread_stopped_on_cancel(self) -> None:
        """Land a hard-cancelled thread in terminal STOPPED, or ABORTED when its owner's
        abort reached it first; no-op if already terminal.

        ``abort_execution`` cancels the task, and ``CancelledError`` is a
        BaseException no handler in ``start`` catches, so without this the
        thread keeps its pre-abort status and the aborted execution goes on
        reporting it as active.
        """
        if self.status.is_terminal():
            return
        try:
            if self._released_by_the_owners_abort():
                self._fire(ThreadEvent.ABORT_THREAD)
                return
            if self.status != LabwareThreadStatus.STOPPING:
                self._fire(ThreadEvent.STOP_REQUESTED)
            self._handle_thread_stop()
        except Exception:
            orca_logger.exception(
                "Thread %s - stop-on-cancel fire raised; cancellation re-raised",
                self._thread.name,
            )

    async def _start_body(self) -> None:
        # Ahead of the acquisition below, because getting a start plate is
        # already physical work. See ``hold_at_start``.
        uninitialized = self.status == LabwareThreadStatus.CREATED
        if self._held_at_start and self._pause_request_event.is_set():
            self._held_at_start = False
            try:
                await self._handle_manual_pause()
            except asyncio.CancelledError:
                # A confirmed stop cancels the task while the thread is parked
                # here, before the loop below has a terminal path to fall into.
                self._fire_thread_stopped_on_cancel()
                raise

        if uninitialized:
            try:
                await self.initialize_labware()
            except asyncio.CancelledError:
                # An abort can land while the thread waits for its start location.
                self._fire_thread_stopped_on_cancel()
                raise
            except _ThreadStopSignal:
                # Getting labware already on the deck to the start is a move, and a stop can land in it.
                self._release_all_held_reservations()
                self._handle_thread_stop()
                self._tell_the_waiting_method_its_labware_is_gone()
                return
            except _ThreadAbortedSignal:
                self._release_all_held_reservations()
                # Aborted at the start dispense: the plate never arrived, so its registry entry goes too.
                if self._labware_registry is not None:
                    self._labware_registry.unregister(self._thread.labware.id)
                self._fire(ThreadEvent.ABORT_THREAD)
                self._tell_the_waiting_method_its_labware_is_gone()
                return

        if self._stop_event.is_set():
            self._handle_thread_stop()
            self._tell_the_waiting_method_its_labware_is_gone()
            return
        self._reached_start = True

        # Yield thread: create MergeLane now that EventChannelRegistry is available
        assert self._event_channel_registry is not None, (
            "EventChannelRegistry must be set before starting a yield thread"
        )
        self._method_lane = MergeLane(
            self._yield_adapter(self._event_channel_registry),
            name_getter=lambda m: m.name,
        )

        try:
            try:
                await self._run_method_loop()
            except _ThreadAbortedSignal:
                # Operator-initiated ABORT_THREAD recovery. Transition to the terminal ABORTED state so anyone awaiting
                # ``self.completed`` unblocks and the workflow loop sees a
                # cleanly-terminal thread, rather than treating the exit as
                # an unhandled exception that would mark the execution
                # FAILED via the task.exception() path.
                self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
                orca_logger.info(
                    "Thread %s - operator aborted via ABORT_THREAD recovery",
                    self._thread.name,
                )
                self._fire(ThreadEvent.ABORT_THREAD)
                self._tell_the_waiting_method_its_labware_is_gone()
            except _ThreadStopSignal:
                if self._released_by_the_owners_abort():
                    orca_logger.info(
                        "Thread %s - the owner aborted the shared action before the stop arrived",
                        self._thread.name,
                    )
                    self._fire(ThreadEvent.ABORT_THREAD)
                else:
                    # Skip the end-location move and land at STOPPED; contributors
                    # follow stop_event directly, so no publish.
                    orca_logger.info(
                        "Thread %s - cooperative stop honored mid-action",
                        self._thread.name,
                    )
                    self._handle_thread_stop()
                    self._tell_the_waiting_method_its_labware_is_gone()
            except asyncio.CancelledError:
                # Hard abort cancels this task; stamp terminal before unwinding.
                self._fire_thread_stopped_on_cancel()
                raise
            except Exception:
                # Owner failed post-binding (move/deadlock/wait-step) without engaging the
                # group pause; release parked contributors so they abort, not strand.
                self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
                self._tell_the_waiting_method_its_labware_is_gone()
                raise
        finally:
            for method in self._completed_methods:
                self._declare_unresolved_anchor_inserts(
                    method.dropped_anchor_inserts, "action")
            if self._assigned_method is not None:
                self._declare_unresolved_anchor_inserts(
                    self._assigned_method.dropped_anchor_inserts, "action")
            method_lane_dropped = await self._method_lane.close()
            self._declare_unresolved_anchor_inserts(method_lane_dropped, "method")
            self._release_all_held_reservations()

    def _tell_the_waiting_method_its_labware_is_gone(self) -> None:
        """This thread was spawned to bring labware to another thread's action and ended before it arrived."""
        if self._bound_to_bring_labware_to is not None:
            self._bound_to_bring_labware_to.note_labware_lost(self._thread.labware.name)

    def _declare_unresolved_anchor_inserts(
        self, dropped: list[DroppedAnchorInsert], target_type: str
    ) -> None:
        """Surface each dropped Before/After insert as a WARNING incident.

        The drop is expected, whether the anchor never ran or the insert
        never got its turn; this only makes it loud. No-op without a declarer
        (threads minted before SystemRuntime binds its declarer).

        Best-effort: this runs in the thread's terminal ``finally``, so a
        declarer failure must never interrupt reservation release or mask
        the exception that ended the thread. Mirrors the guarded declarer
        calls elsewhere in this class.
        """
        if self._thread_incident_declarer is None:
            return
        for item in dropped:
            try:
                self._thread_incident_declarer.declare_unresolved_anchor_insert(
                    self._context.execution_id,
                    self._thread.id,
                    item.anchor_name,
                    item.direction,
                    target_type,
                    item.item_name,
                    item.anchor_reached,
                )
            except Exception:
                orca_logger.warning(
                    "Thread %s - failed to record UNRESOLVED_ANCHOR_INSERT "
                    "for %s anchor %r (dropped insert still discarded)",
                    self._thread.name, target_type, item.anchor_name,
                    exc_info=True,
                )

    def _release_all_held_reservations(self) -> None:
        """Safe-release every reservation still held by this thread on
        terminal exit. Each field is independently guarded so one
        failed release doesn't block the others.
        """
        if self._assigned_action is not None:
            try:
                self._assigned_action.action.release_reservation()
            except ValueError:
                pass
            self._assigned_action = None
        if self._move_action is not None:
            try:
                self._move_action.reservation.release_reservation()
            except ValueError:
                pass
            self._move_action = None
        self._holdover.force_release()

    async def _run_method_loop(self) -> None:
        # consume methods from the lane (generator + insertions)
        while True:
            try:
                self._assigned_method = await self._method_lane.next()
            except StopAsyncIteration:
                break
            except (_ThreadStopSignal, _ThreadAbortedSignal):
                raise
            except Exception as error:
                # A thread-body error pauses under PAUSE policy and surfaces under ABORT,
                # unless it pauses under any policy (see ``error_policy_overrides``).
                if (
                    self._effective_failure_policy() == FailurePolicy.ABORT
                    and not self._pauses_under_any_policy(error)
                ):
                    self._failed_under_abort_policy = True
                    raise error
                decision = await self._pause_for_error(
                    error, f"thread step failed: {type(error).__name__}: {error}",
                    PauseSite.THREAD_STEP,
                )
                if decision == RecoveryDecision.RETRY:
                    continue
                self._check_abort_thread(decision, error)
                raise error

            await self._name_my_partners_or_pause()

            # WaitStep: adapter yields a sentinel with wait_event_name set.
            # Handle the event wait in the thread loop so skip and retry work.
            if self._assigned_method.is_wait_step:
                skipped = self._method_lane.should_skip(self._assigned_method.name)
                if skipped:
                    orca_logger.info(
                        "Thread %s - Skipped event wait: %s",
                        self._thread.name, self._assigned_method.name,
                    )
                    self._assigned_method = None
                    continue
                assert self._event_channel_registry is not None
                wait_event_name = self._assigned_method.wait_event_name
                assert wait_event_name is not None
                await self.wait_for_event(
                    self._event_channel_registry.get_or_create(wait_event_name),
                    self._assigned_method.wait_timeout,
                )
                self._assigned_method = None
                continue

            self._assigned_method.set_current_thread(
                self._thread.id, self._thread.name, self._thread.submission_id, self._thread.group_id,
            )
            if self._event_channel_registry is not None:
                self._assigned_method.set_event_channel_registry(self._event_channel_registry)

            skipped_by_lane = self._method_lane.should_skip(self._assigned_method.name)
            should_skip = self._assigned_method.was_skipped or self._assigned_method.was_aborted or skipped_by_lane
            if should_skip:
                if skipped_by_lane and not self._assigned_method.was_skipped:
                    try:
                        self._assigned_method.mark_skipped()
                    except ValueError:
                        # Shared method race: method became IN_PROGRESS between
                        # skip request and lane consumption. Enter normally.
                        orca_logger.warning(
                            f"Thread {self._thread.name} - Cannot skip '{self._assigned_method.name}' "
                            f"(status={self._assigned_method.status.name}). Entering method normally."
                        )
                        should_skip = False

            if should_skip:
                orca_logger.info(
                    f"Thread {self._thread.name} - "
                    f"{'Aborted' if self._assigned_method.was_aborted else 'Skipped'} "
                    f"method: {self._assigned_method.name}"
                )
                self._completed_methods.append(self._assigned_method)
                self._assigned_method = None
                if self._pause_request_event.is_set():
                    await self._handle_manual_pause()
                continue

            orca_logger.info(f"Thread {self._thread.name} - Starting method: {self._assigned_method.name}")

            while not self._assigned_method.completed.is_set():
                # Step 1: consume next action template (does NOT block on reservation)
                try:
                    unresolved = await self._assigned_method.consume_next_unresolved_action(
                        self._action_resolver
                    )
                except Exception as error:
                    decision = await self._pause_for_error(
                        error, f"action resolution failed: {type(error).__name__}: {error}",
                        PauseSite.ACTION_RESOLUTION,
                    )
                    if decision == RecoveryDecision.RETRY:
                        self._fire(ThreadEvent.RECOVERY_RETRY)
                        continue
                    # No action is bound yet, so the narrower aborts have
                    # nothing to discard and end the thread instead, which is
                    # what HONOURED_DECISIONS says this site does. Falling
                    # through would FAIL the thread and the execution with it.
                    if decision in _ABORTS_THAT_END_AN_UNBOUND_THREAD:
                        decision = RecoveryDecision.ABORT_THREAD
                    self._check_abort_thread(decision, error)
                    raise error

                if unresolved is None:
                    self._holdover.release_current_when_drained()
                    break

                if self._holdover.has_current():
                    potential_locations = self._action_resolver.get_potential_locations(unresolved)
                    self._holdover.maybe_release_for_next_action(potential_locations)

                # Step 2: reserve the device (blocks until granted).
                #
                # Only the OWNER thread (the thread that yields the method
                # directly, not via orca.join) submits the device reservation.
                # The owner's thread_id matches any hold-over reservation on the
                # device, so the request grants re-entrantly. Contributors park on
                # the method's _current_action_resolved event and read the cached
                # _current_action once the owner has bound it. Without this split,
                # a contributor that wins the race to resolve_current_action holds
                # _resolving_action_lock across an indefinite retry loop -- its
                # reservation request carries the contributor's thread_id, which
                # does not match the holder's hold-over and is rejected forever,
                # blocking the owner from ever acquiring the lock to submit its
                # own re-entrant request that would succeed.
                try:
                    if self._assigned_method.shared_coord.is_contributor(self._thread.id):
                        self._assigned_action = await self._wait_for_the_owner_to_bind()
                    else:
                        self._assigned_action = await self._assigned_method.resolve_current_action(
                            self._thread.id,
                            self.current_location,
                            self._action_resolver,
                            requesting_labware=self._thread.labware,
                            status_sink=self,
                        )
                except SharedRendezvousResolved as resolved:
                    # Pre-binding: the owner published a terminal outcome before
                    # binding an action. Follow it without pausing (only the owner pauses).
                    self._react_to_shared_outcome(resolved.outcome)
                    continue
                except AcquisitionYieldRequested:
                    await self._vacate_for_acquisition_yield()
                    continue
                except UnresolvableDeadlockError as error:
                    # Typed catch for unresolvable-deadlock
                    # declarations. Record the typed
                    # IncidentCategory.UNRESOLVABLE_DEADLOCK incident +
                    # fan out pause_all_threads(execution_id) BEFORE
                    # entering the per-thread pause path so the deadlock
                    # surfaces on every operator surface (orca incidents
                    # list / GET /api/incidents) and
                    # the rest of the execution halts cleanly.
                    self._record_unresolvable_deadlock(error)
                    decision = await self._pause_for_error(
                        error, f"action resolution failed: {type(error).__name__}: {error}",
                        PauseSite.DEADLOCK,
                        event=ThreadEvent.UNRESOLVABLE_DEADLOCK,
                    )
                    if decision == RecoveryDecision.RETRY:
                        self._fire(ThreadEvent.RECOVERY_RETRY)
                        continue
                    # No bound action to skip pre-binding: any non-RETRY recovery aborts
                    # the rendezvous (owner + contributor) rather than zombie-park PAUSED.
                    self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
                    raise _ThreadAbortedSignal(str(error))
                except _ThreadStopSignal:
                    raise
                except Exception as error:
                    # ``ActionReservationTimeoutError`` flows
                    # through the same operator-pause path as any other
                    # action-resolution error -- operator chooses
                    # RETRY / ABORT / etc. The typed exception class only
                    # adds structured shape to ``last_error`` so future
                    # envelope translators (when needed) can dispatch on
                    # ``isinstance``; today it is consumed via
                    # ``thread.last_error`` as a string.
                    decision = await self._pause_for_error(
                        error, f"action resolution failed: {type(error).__name__}: {error}",
                        PauseSite.ACTION_RESOLUTION,
                    )
                    if decision == RecoveryDecision.RETRY:
                        self._fire(ThreadEvent.RECOVERY_RETRY)
                        continue
                    # No bound action to skip pre-binding: any non-RETRY recovery aborts
                    # the rendezvous (owner + contributor) rather than zombie-park PAUSED.
                    self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
                    raise _ThreadAbortedSignal(str(error))

                orca_logger.info(
                    f"Thread {self._thread.name} - Action resolved: {self._assigned_action.action.command} "
                    f"at {self._assigned_action.action.location.name}"
                )

                # Pool may have resolved to a different physical device than
                # the held reservation (e.g., shaker_collection). Release if so.
                self._holdover.maybe_release_for_next_action(
                    {self._assigned_action.action.location}
                )

                left_out = self._why_a_left_out_plate_blocks_the_action(unresolved)
                if left_out:
                    await self._pause_for_labware_that_will_not_come(CoLabwareLostError(left_out))
                    continue

                # Spawn contributors only after the owner holds the device; a
                # pre-reservation spawn floods the single-lane bridges (deadlock).
                await self._auto_spawn_with_recovery(unresolved)

                target_sites = self._resolve_action_target_sites(unresolved)
                self._assigned_action.action.set_site_correlation({
                    template.name: f"{self._assigned_action.action.location.position_id}/{site}"
                    for template, site in unresolved.deck_positions.items()
                })
                if self._is_a_joiner_the_action_does_not_take(unresolved):
                    await self._hold_before_the_next_command()
                    await self._follow_shared_action_outcome()
                else:
                    while self.current_location not in target_sites:
                        await self._hold_before_the_next_command()
                        await self._resolve_and_execute_move(target_sites)

                    await self._hold_before_the_next_command()
                    await self._handle_thread_at_assigned_action_location()

                if self._pause_request_event.is_set():
                    await self._handle_manual_pause()

            orca_logger.info(f"Thread {self._thread.name} - Method completed: {self._assigned_method.name}")
            self._completed_methods.append(self._assigned_method)
            self._assigned_method = None

            # Cooperative pause: check between methods
            if self._pause_request_event.is_set():
                await self._handle_manual_pause()

        # all methods in the thread are completed, now move to end location
        await self._handle_thread_completion()

    async def _hold_before_the_next_command(self) -> None:
        """Honour a pending pause before the next move leg or action starts; a stop there ends the thread."""
        if self._pause_request_event.is_set():
            await self.hold_for_pause()

    async def _resolve_and_execute_move(self, target_sites: list[Location]) -> None:
        assert self.assigned_action is not None, "Assigned action should not be None when moving to assigned location"
        await self._fire_and_execute_move_to(
            ThreadEvent.MOVE_RESERVATION_REQUESTED,
            target_sites,
            assigned_action=self.assigned_action.action,
        )

    def _resolve_action_target_sites(
        self, unresolved: UnresolvedLocationAction
    ) -> list[Location]:
        """The candidate sites THIS thread's labware may occupy for the
        bound action: declared deck_positions site -> the
        device's single site -> stay-in-place for labware already on the
        device -> ALL interchangeable working sites (the reservation grant
        picks a free one, so two undeclared inputs never race a snapshot).
        Multi-site decks require a declaration; handoffs never qualify."""
        assert self._assigned_action is not None
        mutex = self._assigned_action.action.location
        sites = self._move_handler.system_map.sites_of(mutex.position_id)
        if not sites:
            return [mutex]
        labware = self._thread.labware
        template = labware.template if labware is not None else None
        declared = unresolved.deck_positions.get(template) if template is not None else None
        working = sites
        expected = len(unresolved.expected_input_templates)
        if expected > len(working):
            raise ValueError(
                f"Action '{self._assigned_action.action.command}' expects "
                f"{expected} labware inputs but device '{mutex.position_id}' owns "
                f"only {len(working)} working site(s) - each input needs its own "
                f"physical position (single occupancy; a shaker-class device "
                f"cannot host a multi-input convergence)."
            )
        if declared is not None:
            node_id = f"{mutex.position_id}/{declared}"
            for site in working:
                if site.position_id == node_id:
                    return [site]
            self._raise_missing_deck_site(mutex, template, working, declared)
        if len(working) == 1:
            return working
        if self.current_location.owner_mutex_id == mutex.position_id:
            return [self.current_location]
        if any(isinstance(s.resource, DeviceDeckSite) for s in working):
            self._raise_missing_deck_site(mutex, template, working, None)
        return working

    def _raise_missing_deck_site(
        self,
        mutex: Location,
        template: LabwareTemplate | None,
        working: List[Location],
        declared: str | None,
    ) -> NoReturn:
        assert self._assigned_action is not None
        raise TransitLabwareMissingDeckSiteError(
            self._thread.name, mutex.position_id,
            template.name if template is not None else "<no-template>",
            [s.position_id for s in working],
            # The fix is a decorator argument, so the refusal has to say which
            # decorator: a thread yields several actions and only one is wrong.
            self._assigned_action.action.command,
            declared_site=declared,
        )

    async def _fire_and_execute_move_to(
        self,
        event: ThreadEvent,
        targets: list[Location],
        assigned_action: LocationAction | None = None,
        escape_on_arrival: bool = True,
        abandon_when: Callable[[], bool] | None = None,
    ) -> None:
        """Get this thread's labware to one of ``targets``.

        A plan freezes the source it was built from, and the grant that lets it
        run can be hours later, so an operator has every chance to move the
        plate in between. When one does, the plan is thrown away and a new one
        is built from wherever the plate now is, queueing for reservations
        behind everyone else exactly like a first attempt.
        """
        self._fire(event)
        replanned = False
        try:
            while True:
                self._labware_moved_event.clear()
                if self._labware_discharged:
                    await self._pause_before_replanning(LabwareDischargedError(self._thread.labware.name))
                    continue
                self._planned_from = self.current_location
                previous_location = self._location_history.get_previous_location()
                try:
                    self._move_action = await self._move_handler.resolve_move_action(
                        self._thread.id,
                        self._thread.labware,
                        self._planned_from,
                        targets,
                        assigned_action,
                        previous_location,
                        escape_on_arrival,
                        self._withdraw_move_when(abandon_when),
                    )
                except MoveAbandonedError:
                    if abandon_when is not None and abandon_when():
                        # The caller's own reason to withdraw wins: its park is
                        # moot however the labware moved, and nothing is held.
                        self._fire(ThreadEvent.PARK_ABANDONED)
                        raise
                    if self._pause_request_event.is_set() and not self._plan_is_stale():
                        await self.hold_for_pause()
                        self._fire(event)
                        continue
                except Exception as error:
                    if not replanned and not isinstance(error, (NoRouteError, RouteGraphMissingTeachpointError)):
                        raise
                    # No route reaches where it is or where it must go. Letting
                    # this escape would kill the thread instead of asking.
                    await self._pause_before_replanning(error)
                    continue
                else:
                    if await self._execute_move_action():
                        return
                    if self._pause_request_event.is_set() and not self._plan_is_stale():
                        # Give back what the plan held, pause, then plan again from the same place.
                        self._discard_stale_move()
                        await self.hold_for_pause()
                        self._fire(event)
                        continue
                self._discard_stale_move()
                self._fire(ThreadEvent.MOVE_REPLAN_REQUESTED)
                replanned = True
                if self._stop_event.is_set():
                    # A stop landed while the plan was being torn up. Stop
                    # wins; the thread is not about to start another journey.
                    raise _ThreadStopSignal()
                if self.current_location in targets:
                    # The operator carried it to another of this move's
                    # candidate targets. Step out as an arrival: a route from a
                    # site to itself is not a route, and there is nothing left
                    # to carry.
                    self._move_handler.release_stale_corridor_holds(
                        self._thread.id, self.current_location,
                    )
                    self._fire(ThreadEvent.MOVE_TARGET_AWAITED)
                    self._fire(ThreadEvent.MOVE_TARGET_GRANTED)
                    self._holdover.release_after_move_if_drained()
                    return
        finally:
            self._planned_from = None

    async def _pause_before_replanning(self, error: Exception) -> None:
        """Park at the move site when no move can be planned: no route, or the labware was discharged.

        RETRY plans again from wherever the operator now says the labware is. A
        discharged labware cannot come back, so only ABORT_THREAD ends that pause.
        There is no move bound to declare finished, so CONTINUE is refused.
        """
        message = f"no move could be planned, {type(error).__name__}: {error}"
        decision = await self._pause_for_error(
            error, message, PauseSite.MOVE_RESOLUTION,
        )
        self._fire(ThreadEvent.MOVE_RETRY)
        if decision != RecoveryDecision.RETRY:
            self._check_abort_thread(decision, error)
            raise error
        self._fire(ThreadEvent.MOVE_REPLAN_REQUESTED)

    def _withdraw_move_when(
        self, abandon_when: Callable[[], bool] | None,
    ) -> Callable[[], bool]:
        """Withdraw the pending reservation request on the caller's own
        condition, when the thread is stopped, once the plan being requested no
        longer starts where the labware is, or when the run asks the thread to pause."""
        def withdraw() -> bool:
            return (
                self._stop_event.is_set()
                or self._plan_is_stale()
                or self._pause_request_event.is_set()
                or (abandon_when is not None and abandon_when())
            )
        return withdraw

    def _plan_is_stale(self) -> bool:
        """True when the ledger puts the labware somewhere this plan does not
        account for.

        A plan accounts for three positions: the source it collects from, the
        target it delivers to, and the jaws of the mover carrying it in
        between. Anywhere else means someone has moved the plate and the plan
        cannot be run as it stands. Asking the ledger rather than watching for
        an edit means re-confirming a position the labware already had is not
        a move, and a signal that arrives late is still answered correctly.
        """
        if self._labware_discharged:
            return True
        planned_from = self._planned_from
        if planned_from is None:
            return False
        here = self._ledger_position(self._thread.labware)
        if here is None:
            # Nothing recorded: the plan is the only statement of where it is.
            return False
        if here.position_id == planned_from.position_id:
            return False
        move = self._move_action
        if move is None:
            return True
        return here.position_id not in {
            move.target.position_id,
            move.transporter.gripper_location.position_id,
        }

    def _discard_stale_move(self) -> None:
        """Drop a resolved move whose source no longer holds the labware.

        Only a hold the move owns is released. When the destination reservation
        belongs to the action the move was feeding, that action still wants it
        and the next plan is handed the same one.
        """
        move = self._move_action
        if move is None:
            return
        if move.release_reservation_on_place:
            move.reservation.release_reservation()
        self._move_action = None

    def notify_labware_moved(self) -> None:
        """Wake this thread to re-read where its labware is.

        An operator write calls this after recording a new position. It only
        wakes: whether the plan is stale is decided by reading the ledger, so a
        wake that turns out to change nothing costs one re-check.
        """
        self._labware_moved_event.set()

    def notify_labware_discharged(self) -> None:
        """An operator forced this thread's labware off the record. It pauses at its next step."""
        self._labware_discharged = True
        self._labware_moved_event.set()

    @property
    def co_labware_timeout(self) -> float | None:
        return self._coordination_config.co_labware_timeout

    def _event_wait_timeout(self, step_timeout: float | None) -> float | None:
        return step_timeout if step_timeout is not None else self._coordination_config.event_timeout

    async def wait_for_event(self, channel: EventChannel, step_timeout: float | None) -> str | None:
        timeout = self._event_wait_timeout(step_timeout)
        while True:
            try:
                _, value, _ = await channel.wait(seen_counter=0, timeout=timeout)
                return value
            except TimeoutError as err:
                # A stdlib TimeoutError never pauses under ABORT; the check
                # matches the other policy-ABORT branches.
                if (
                    self._effective_failure_policy() == FailurePolicy.ABORT
                    and not self._pauses_under_any_policy(err)
                ):
                    self._failed_under_abort_policy = True
                    raise
                decision = await self._pause_for_error(
                    err,
                    f"thread step failed: wait event {channel.name!r} timed out after {timeout}s",
                    PauseSite.WAIT_EVENT_TIMEOUT,
                )
                if decision == RecoveryDecision.RETRY:
                    self._fire(ThreadEvent.RECOVERY_RETRY)
                    continue
                self._check_abort_thread(decision, err)
                raise

    def _shared_action_group(self) -> SharedActionCoordination | None:
        """The action-scoped participant group iff this thread's current action is
        shared (the method has live contributors). None for a non-shared action, where
        recovery stays per-thread and single-thread pause/resume is unaffected."""
        method = self._assigned_method
        if method is None:
            return None
        if not method.shared_coord.is_shared:
            return None
        return method.current_action_coord

    async def _follow_shared_action_outcome(self) -> None:
        """Contributor path for a shared action. The owner drives the device op; this
        thread's labware is already at the device, so it joins the action group and
        awaits the group's one outcome. If the owner fails the action the group pauses:
        this contributor transitions to PAUSED too (so the operator sees the whole
        action stuck), records no incident, and stays paused until the group's single
        recovery decision resolves the action -- then it fans out to that outcome."""
        assert self._assigned_action is not None
        assert self._assigned_method is not None
        group = self._assigned_method.current_action_coord
        assert group is not None, "action group must be minted before a contributor waits"
        if self._stop_event.is_set():
            self._assigned_action = None
            raise _ThreadStopSignal()
        self._fire(ThreadEvent.CO_LABWARE_AWAITED)
        self._fire(ThreadEvent.ACTION_RESOLVED)
        paused_here = False
        self._following_peer_action = True
        try:
            while not group.outcome.done():
                waiters = [
                    asyncio.ensure_future(self._stop_event.wait()),
                    asyncio.ensure_future(_settled(group.outcome)),
                ]
                if not paused_here:
                    waiters.append(asyncio.ensure_future(group.action_paused.wait()))
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    await drain_cancelled_waiters(*waiters)
                if group.outcome.done() or self._stop_event.is_set():
                    break
                if not paused_here and group.action_paused.is_set():
                    error = _SharedActionPausedError(
                        self._assigned_action.action.command
                    )
                    self._last_error = error
                    self._pause_message = str(error)
                    # The group's site, not this thread's: a contributor drives
                    # no call, and the one decision the group takes is judged
                    # against where the OWNER stopped. Same reasoning as
                    # `paused_device_command`.
                    self._pause_site = (
                        group.step_paused_site or PauseSite.ACTION_BODY
                    )
                    self._fire(ThreadEvent.ERROR_PAUSE)
                    paused_here = True
        except asyncio.CancelledError:
            # An ABORT-policy owner publishes ABORT_THREAD then re-raises, cancelling
            # this task before it reads the outcome; honor it (ABORTED) not frozen.
            if self._released_by_the_owners_abort():
                self._assigned_action = None
                raise _ThreadAbortedSignal("shared rendezvous aborted by the owner")
            raise
        finally:
            self._following_peer_action = False
        self._assigned_action = None
        if self._stop_event.is_set() or not group.outcome.done():
            raise _ThreadStopSignal()
        if paused_here:
            self._fire(ThreadEvent.RESUME_REQUESTED)
        outcome = group.outcome.result()
        if paused_here and outcome.decision != RecoveryDecision.ABORT_THREAD:
            self._last_error = None
            self._pause_message = None
            self._pause_site = None
        self._react_to_shared_outcome(outcome)

    def _owners_abort_published(self) -> bool:
        method = self._assigned_method
        if method is None or not method.shared_coord.is_contributor(self._thread.id):
            return False
        group = method.current_action_coord
        return (
            group is not None
            and group.outcome.done()
            and group.outcome.result().decision == RecoveryDecision.ABORT_THREAD
        )

    def _released_by_the_owners_abort(self) -> bool:
        """The owner's abort reached this contributor before any stop did."""
        return self._owners_abort_published() and not self._stopped_before_the_owners_abort

    def _react_to_shared_outcome(self, outcome: ActionResolution) -> None:
        """React to the group's published outcome. ABORT_THREAD tears this contributor
        down; every other outcome returns so the method loop advances to the next action
        or exits on ``completed`` (ABORT_METHOD / completion)."""
        if outcome.decision == RecoveryDecision.ABORT_THREAD:
            raise _ThreadAbortedSignal("shared rendezvous aborted by the owner")

    def _publish_owner_slot_outcome_on_exit(self, decision: RecoveryDecision) -> None:
        """Publish a terminal action outcome so contributors fan out when the OWNER
        leaves the rendezvous outside the normal funnels (handle_recovery / completion):
        a resolution error, an abort at a co-labware timeout, or a cooperative stop. Idempotent;
        skipped for contributors (they follow, never drive) and when no group is minted."""
        method = self._assigned_method
        if method is None:
            return
        if method.shared_coord.is_contributor(self._thread.id):
            return
        group = method.current_action_coord
        if group is not None:
            group.publish_outcome(ActionResolution(decision))
        if decision == RecoveryDecision.ABORT_METHOD:
            # The method is over, and saying so is what stops a contributor
            # asking it for another action: it would be handed the same bound
            # one and the same finished outcome, with no await in between.
            method.shared_coord.exit_signal.set()
            method.shared_coord.completed.set()

    def _why_a_left_out_plate_blocks_the_action(self, unresolved: UnresolvedLocationAction) -> str:
        """Why the action can never start, when it needs the plate of an optional thread the group left out."""
        assert self._assigned_action is not None and self._assigned_method is not None
        if self._assigned_method.shared_coord.is_contributor(self._thread.id):
            return ""
        missing = {template.name for template in self._inputs_with_no_thread(unresolved)}
        command = self._assigned_action.action.command
        return " ".join(
            f"Thread {self._thread.name}: action '{command}' needs plate '{labware}', and its group left out "
            f"the optional thread '{thread}' that brings it, so it will never come. Submit again with "
            f"'{thread}' named in the group, or declare it with wf.thread so it is spawned when an action "
            f"needs it. ABORT_ACTION skips the action, ABORT_THREAD ends this plate."
            for labware, thread in self._thread.left_out.items() if labware in missing
        )

    async def _pause_for_labware_that_will_not_come(self, error: CoLabwareLostError) -> None:
        assert self._assigned_method is not None
        decision = await self._pause_for_error(error, str(error), PauseSite.CO_LABWARE_LOST)
        self._fire(_recovery_event_for(decision))
        await self._assigned_method.handle_recovery(decision)
        self._assigned_action = None
        self._check_abort_thread(decision, error)

    async def _handle_thread_at_assigned_action_location(self) -> None:
        assert self._assigned_action is not None
        assert self._assigned_method is not None
        assert (
            self.current_location.owner_mutex_id
            == self._assigned_action.action.location.position_id
            or self.current_location == self._assigned_action.action.location
        )

        if self._assigned_method.shared_coord.is_contributor(self._thread.id):
            await self._follow_shared_action_outcome()
            return

        while True:
            self._assigned_action.action.refresh_labware_presence()
            self._assigned_method.shared_coord.signal_lost_among(set(self._assigned_action.action.missing_input_report()))
            self._fire(ThreadEvent.CO_LABWARE_AWAITED)
            action = self._assigned_action.action
            self._move_handler.note_co_labware_wait(
                self._thread.id, lambda: {labware.id for labware in action.peek_missing_input_labware()},
            )
            try:
                outcome = await CoLabwareCoordinator.wait(
                    stop_event=self._stop_event,
                    exit_event=self._assigned_method.exit_signal,
                    pause_event=self._pause_request_event,
                    co_labware_event=action.all_labware_is_present,
                    labware_lost_event=self._assigned_method.shared_coord.labware_lost,
                    timeout=self.co_labware_timeout,
                )
            finally:
                self._move_handler.end_co_labware_wait(self._thread.id)

            if outcome is CoLabwareWaitOutcome.CO_LABWARE_LOST:
                missing = set(self._assigned_action.action.missing_input_report())
                lost = self._assigned_method.shared_coord.take_lost_labware(missing)
                if not lost:
                    continue
                await self._pause_for_labware_that_will_not_come(CoLabwareLostError(
                    f"Thread {self._thread.name}: {', '.join(lost)} ended before reaching "
                    f"{self._assigned_action.action.location.name} for action "
                    f"'{self._assigned_action.action.command}'. ABORT_ACTION skips the action, "
                    f"ABORT_THREAD ends this plate."
                ))
                return

            if outcome is CoLabwareWaitOutcome.STOP_REQUESTED:
                # Stop is per-thread; only the reservation owner releases.
                # Contributors in a shared method share the owner's
                # ExecutableLocationAction object -- releasing here would
                # drop the still-alive owner's device reservation.
                if self._assigned_action.action.reservation.thread_id == self._thread.id:
                    self._assigned_action.action.release_reservation()
                self._assigned_action = None
                raise _ThreadStopSignal()

            if outcome is CoLabwareWaitOutcome.PAUSE_REQUESTED:
                # A cancel_pending_pause between the wake and this turn wins
                # (the cancel contract): re-enter the wait, do not latch PAUSED.
                if self._pause_request_event.is_set():
                    await self._handle_manual_pause()
                continue

            if outcome is CoLabwareWaitOutcome.METHOD_EXIT:
                orca_logger.info(
                    f"Thread {self._thread.name} - Method '{self._assigned_method.name}' "
                    f"{'aborted' if self._assigned_method.was_aborted else 'skipped'} "
                    f"while awaiting co-labware at {self._assigned_action.action.location.name}"
                )
                # Symmetric with the STOP_REQUESTED guard above: contributors
                # share the owner's ExecutableLocationAction; only the owner
                # releases the underlying device reservation.
                if self._assigned_action.action.reservation.thread_id == self._thread.id:
                    self._assigned_action.action.release_reservation()
                self._assigned_action = None
                return

            if outcome is CoLabwareWaitOutcome.TIMEOUT:
                missing_names = ", ".join(
                    self._assigned_action.action.missing_input_report()
                ) or "unknown"
                timed_out = TimeoutError(
                    f"Thread {self._thread.name} timed out ({self.co_labware_timeout}s) waiting "
                    f"for co-labware at {self._assigned_action.action.location.name}. "
                    f"Missing: {missing_names}"
                )
                self._record_co_labware_timeout()
                decision = await self._pause_for_error(timed_out, str(timed_out), PauseSite.CO_LABWARE_WAIT)
                if decision == RecoveryDecision.RETRY:
                    self._fire(ThreadEvent.RECOVERY_RETRY)
                    continue
                # An arrived contributor aborts cleanly rather than zombie-parking.
                self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
                raise _ThreadAbortedSignal(str(timed_out))

            break

        # An abort can land between the co-labware wakeup and this segment
        # (accept-partial drain); honor it instead of driving the dead method.
        if self._assigned_method.exit_signal.is_set():
            orca_logger.info(
                f"Thread {self._thread.name} - Method '{self._assigned_method.name}' "
                f"{'aborted' if self._assigned_method.was_aborted else 'skipped'} "
                f"between co-labware wakeup and execution"
            )
            if self._assigned_action.action.reservation.thread_id == self._thread.id:
                self._assigned_action.action.release_reservation()
                self._assigned_action = None
            return

        self._fire(ThreadEvent.ACTION_RESOLVED)
        orca_logger.info(
            f"Thread {self._thread.name} - Executing: {self._assigned_action.action.command} "
            f"at {self._assigned_action.action.location.name}"
        )

        # This thread drives the body, so it is the one whose pause a safe
        # point inside the body has to honor.
        self._assigned_action.set_pause_checkpoint(self)
        method = self._assigned_method
        if method is not None:
            self._assigned_action.require_partner_barcodes(lambda: method.partner_barcodes)
        try:
            if self._labware_discharged:
                raise LabwareDischargedError(self._thread.labware.name)
            await self._assigned_action.execute()
        except Exception as error:
            await self._handle_action_error(error)
            return

        orca_logger.info(
            f"Thread {self._thread.name} - Action completed: {self._assigned_action.action.command} "
            f"at {self._assigned_action.action.location.name}"
        )
        await asyncio.sleep(0)

        tip_context = ThreadExecutionContext(
            execution_id=self._context.execution_id,
            workflow_name=self._context.workflow_name,
            thread_id=self._thread.id,
            thread_name=self._thread.name,
            template_name=self._thread.template_name,
            group_id=self._thread.group_id,
        )
        await CoLabwareCoordinator.emit_tip_events(
            self._assigned_action, tip_context, self._event_bus,
        )

        owns_reservation = (
            self._assigned_action.action.reservation.thread_id == self._thread.id
        )
        self._holdover.acquire_after_action(self._assigned_action, owns_reservation)
        self._assigned_action = None

    def _record_unresolvable_deadlock(
        self, error: UnresolvableDeadlockError,
    ) -> None:
        """Record an unresolvable-deadlock incident via the runtime declarer (S3 R1.5).

        Invoked from the typed ``except UnresolvableDeadlockError:`` arm
        in the action-resolution loop. When a declarer is wired in
        (the runtime's ``IncidentDeclarer`` in production), this records an incident under
        ``IncidentCategory.UNRESOLVABLE_DEADLOCK`` with the rich
        diagnostic context AND fans out
        ``pause_all_threads(execution_id)`` so the rest of the
        execution halts cleanly at safe points.

        When the declarer is ``None`` (test fixtures that build threads
        directly without a runtime), the call is a silent skip -- the
        per-thread pause path still fires via the caller's
        ``_pause_for_error``.

        Incident-recording failures are logged but do not propagate;
        masking the underlying deadlock from the operator pause path
        would defeat the point of the catch.
        """
        if self._thread_incident_declarer is None:
            return
        try:
            self._thread_incident_declarer.declare_unresolvable_deadlock(
                self._context.execution_id,
                error.context,
            )
        except Exception:
            orca_logger.exception(
                "declare_unresolvable_deadlock failed; "
                "falling through to per-thread pause"
            )

    async def _recover_device_op(
        self, error: Exception, command: str, site: PauseSite,
    ) -> RecoveryDecision:
        """Operation-level recovery seam, consulted by the device dispatcher when a
        device call in this thread's action body fails. Records the failure, pauses
        the thread for an operator decision, and returns it: RETRY makes the
        dispatcher re-invoke just the failed call (the body stays suspended at its
        await); an action-level decision (CONTINUE / ABORT_*) is applied by
        ``_handle_action_error`` via the ``OperationDecisionSignal`` the dispatcher
        then raises.

        On a shared action the owner drives the op alone, so only it pauses here; the
        decision is published to contributors as the slot's set-once outcome. Contributors
        never enter this seam (they await the outcome, they do not run the op).
        """
        assert self._assigned_method is not None
        if (
            self._assigned_method.current_failure_policy == FailurePolicy.ABORT
            and not self._pauses_under_any_policy(error)
        ):
            # ABORT policy has no operator loop; propagate for whole-action abort.
            # The body may still catch it, so the thread is marked only when it leaves the action.
            raise error
        self._record_action_failure(error, command)
        # The failure itself is the command's COMMAND.<device>.FAILED; the thread's pause says where it stopped.
        is_device_op = site in (PauseSite.DEVICE_OP, PauseSite.COMMAND_SAVE)
        if site is PauseSite.COMMAND_SAVE:
            message = (
                f"device op '{command}' ran, and saving it failed with {type(error).__name__}: "
                f"{str(error).rstrip('.')}. RETRY saves it again and sends nothing to the device."
            )
        elif is_device_op:
            message = f"device op '{command}' failed with {type(error).__name__}: {error}"
        else:
            message = f"'{command}' ran out of time; RETRY waits again"
        if is_device_op:
            self._op_pause_command = command
        group = self._shared_action_group()
        if group is not None:
            group.mark_paused()
            group.mark_step_paused(site, command if is_device_op else None)
        try:
            decision = await self._pause_for_error(error, message, site)
        finally:
            self._op_pause_command = None
            if group is not None:
                group.clear_step_paused()
        if decision == RecoveryDecision.RETRY:
            # Back into the action body to repeat the step; action-level decisions
            # (CONTINUE/ABORT_*) un-pause in _handle_action_error after the body unwinds.
            self._fire(ThreadEvent.ACTION_BODY_RESUMED)
        return decision

    def _record_move_failure(self, error: Exception) -> None:
        """Record a MOVE_FAILED incident via the runtime declarer.

        The move-side mirror of ``_record_action_failure``: invoked from the
        PAUSE branch of ``_execute_move_action`` just before the thread parks.
        A move failure otherwise reached ``_pause_for_error`` with no queryable
        record, so an auto-pause on a wedged move was undiagnosable. Silent skip
        when no declarer is wired (test fixtures build threads directly);
        recording failures are logged, never propagated.
        """
        if self._thread_incident_declarer is None:
            return
        assert self._move_action is not None
        context = MoveFailedContext(
            source=self._move_action.source.position_id,
            target=self._move_action.target.position_id,
            transporter=self._move_action.transporter.name,
            labware=self._move_action.labware.name,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        try:
            self._thread_incident_declarer.declare_move_failure(
                self._context.execution_id,
                self._thread.id,
                context,
            )
        except Exception:
            orca_logger.exception(
                "declare_move_failure failed; "
                "falling through to per-thread pause"
            )

    def _record_move_continued(self, error: Exception) -> None:
        """Record a MOVE_CONTINUED incident when an operator finished the move.

        The move-side mirror of ``_record_action_continued``. The MOVE_FAILED
        incident stays as it is: the arm did fail, and this only adds that the
        labware reached the target by hand, so an operator reading incidents
        later sees which arrival rests on a person's word. Same silent-skip and
        never-propagate contract as ``_record_move_failure``.
        """
        if self._thread_incident_declarer is None:
            return
        assert self._move_action is not None
        context = MoveContinuedContext(
            source=self._move_action.source.position_id,
            target=self._move_action.target.position_id,
            transporter=self._move_action.transporter.name,
            labware=self._move_action.labware.name,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        try:
            self._thread_incident_declarer.declare_move_continued(
                self._context.execution_id,
                self._thread.id,
                context,
            )
        except Exception:
            orca_logger.exception(
                "declare_move_continued failed; the move still continues"
            )

    def _record_action_failure(
        self, error: Exception, device_command: str | None,
    ) -> None:
        """Record an ACTION_FAILED incident via the runtime declarer.

        Invoked from the PAUSE branch of ``_handle_action_error`` just
        before the thread parks in ``_pause_for_error``. Mirrors
        ``_record_unresolvable_deadlock``: when a declarer is wired in
        (the runtime's ``IncidentDeclarer`` in production) it adds a queryable
        ``IncidentCategory.ACTION_FAILED`` record so the failure surfaces
        on every operator surface. When the declarer is ``None`` (test
        fixtures that build threads directly) it is a silent skip -- the
        pause path still fires. Recording failures are logged, never
        propagated, so they cannot mask the underlying action error.

        ``device_command`` is the call the body is suspended inside, passed by
        the op-level recovery seam and None on the whole-action path. It is what
        makes the incident advise the retry the runtime will actually accept.
        """
        if self._thread_incident_declarer is None:
            return
        assert self._assigned_method is not None
        assert self._assigned_action is not None
        context = ActionFailedContext(
            action_command=self._assigned_action.action.command,
            method_name=self._assigned_method.name,
            error_type=type(error).__name__,
            error_message=str(error),
            device_command=device_command,
        )
        try:
            self._thread_incident_declarer.declare_action_failure(
                self._context.execution_id,
                self._thread.id,
                context,
            )
        except Exception:
            orca_logger.exception(
                "declare_action_failure failed; "
                "falling through to per-thread pause"
            )

    def _record_auto_spawn_failure(self, unresolved: UnresolvedLocationAction, error: Exception) -> None:
        """Record an AUTO_SPAWN_FAILED incident; same silent-skip-and-never-propagate contract as the others."""
        if self._thread_incident_declarer is None:
            return
        requested = ", ".join(template.name for template in self._inputs_with_no_thread(unresolved))
        context = AutoSpawnFailedDetail(
            requested_labware_name=requested or "unknown", requesting_thread_id=self._thread.id,
        )
        try:
            self._thread_incident_declarer.declare_auto_spawn_failure(
                self._context.execution_id, context, f"{type(error).__name__}: {error}",
            )
        except Exception:
            orca_logger.exception("declare_auto_spawn_failure failed; the thread still pauses")

    def _record_co_labware_timeout(self) -> None:
        """Record a CO_LABWARE_TIMEOUT incident; same silent-skip-and-never-propagate contract as the others."""
        if self._thread_incident_declarer is None or self._assigned_action is None:
            return
        context = CoLabwareTimeoutDetail(
            missing_labware_names=tuple(self._assigned_action.action.missing_input_report()),
            waited_seconds=self.co_labware_timeout or 0.0,
            position_id=self._assigned_action.action.location.name,
        )
        try:
            self._thread_incident_declarer.declare_co_labware_timeout(
                self._context.execution_id, self._thread.id, context,
            )
        except Exception:
            orca_logger.exception("declare_co_labware_timeout failed; the thread still pauses")

    def _record_action_continued(self, error: BaseException, unsaved: UnsavedResult | None = None) -> None:
        """Record an ACTION_CONTINUED incident when the operator carries on.

        The ACTION_FAILED incident stays as it is: continuing past a failure is
        not resolving it. This adds the queryable statement that the run went on
        with the action's side effects unknown, so an operator reading incidents
        later sees which stretch of the run was never checked against the device.
        Same silent-skip-and-never-propagate contract as
        ``_record_action_failure``.
        """
        if self._thread_incident_declarer is None:
            return
        assert self._assigned_method is not None
        assert self._assigned_action is not None
        context = ActionContinuedContext(
            action_command=self._assigned_action.action.command,
            method_name=self._assigned_method.name,
            error_type=type(error).__name__,
            error_message=str(error),
            unsaved_command=None if unsaved is None else unsaved.command,
            unsaved_request=None if unsaved is None else unsaved.request,
            unsaved_result=None if unsaved is None else unsaved.result,
        )
        try:
            self._thread_incident_declarer.declare_action_continued(
                self._context.execution_id,
                self._thread.id,
                context,
            )
        except Exception:
            orca_logger.exception(
                "declare_action_continued failed; the thread still continues"
            )

    async def _pause_for_error(
        self,
        error: Exception,
        message: str,
        site: PauseSite,
        event: ThreadEvent = ThreadEvent.ERROR_PAUSE,
    ) -> RecoveryDecision:
        """Pause the thread on an error and wait for operator decision.

        ``site`` is WHERE this pause happened. Every site honours a different
        set of decisions and the wire carried only the error text, so a client
        had to send one to find out which -- at a move that costs the execution.

        ``event`` selects the cause-named TSM transition (default
        ``ERROR_PAUSE``; the ``UnresolvableDeadlockError`` catch passes
        ``UNRESOLVABLE_DEADLOCK``). Both resolve to ``PAUSED`` and the
        published status event is ``THREAD.<id>.PAUSED`` either way --
        the cause is local to the state machine, not on the wire.
        ``_last_error`` is cleared on every decision the thread survives
        (``RETRY``, ``RETRY_ACTION``, ``CONTINUE``, ``ABORT_ACTION``,
        ``ABORT_METHOD``) because the thread keeps running and a later clean
        completion must not leak the recovered error onto its snapshot. Only
        ``ABORT_THREAD`` (terminal) preserves it, so ``thread.last_error`` keeps
        attributing the abort to its cause after the thread lands ``ABORTED``.
        """
        # The longest wait in the system: it ends when a person decides. The sites inside an
        # action keep its device, because RETRY resumes there.
        if site not in (
            PauseSite.ACTION_BODY, PauseSite.DEVICE_OP, PauseSite.COMMAND_SAVE, PauseSite.ACTION_WAIT,
            PauseSite.CO_LABWARE_WAIT, PauseSite.CO_LABWARE_LOST,
        ):
            self._holdover.release_current()
        self._last_error = error
        self._pause_message = message
        self._pause_site = site
        self._resume_event.clear()
        self._decided_by_group = self._group_deciding(site)
        self._fire(event)
        orca_logger.error("Thread %s paused: %s", self.name, message)
        # A paused thread is exactly when a person reaches into the deck, so
        # what this labware holds stops being something the record can vouch for.
        await self._thread.labware.note_observation_gap(
            ObservationGapCause.ERROR_PAUSE,
        )

        if self._event_channel_registry is not None:
            channel = self._event_channel_registry.get_or_create("__workflow_error__")
            await channel.publish(
                value="thread_error",
                data={
                    "thread_id": self._thread.id,
                    "thread_name": self._thread.name,
                    "error": str(error),
                },
            )

        decision = await self._await_recovery_decision()
        orca_logger.info("Thread %s resuming with decision: %s", self.name, decision.name)

        self._recovery_decision = None
        if decision != RecoveryDecision.ABORT_THREAD:
            self._last_error = None
            self._pause_message = None
            self._pause_site = None
        return decision

    def _group_deciding(self, site: PauseSite | None) -> SharedActionCoordination | None:
        """The paused action group that decides this pause, or None when the thread decides alone."""
        group = self._shared_action_group()
        if group is not None and group.action_paused.is_set() and site in (
            PauseSite.ACTION_BODY, PauseSite.DEVICE_OP, PauseSite.COMMAND_SAVE, PauseSite.ACTION_WAIT,
        ):
            return group
        return None

    async def _await_recovery_decision(self) -> RecoveryDecision:
        """Wait for the recovery decision, from the right channel.

        A shared action that paused as a group takes ONE decision, fed by any
        participant, and the group's pause is always at the action body or one
        step inside it (a device call, its save, or a wait): only those sites mark it.

        The group is fixed when the pause begins (`_group_deciding`), from the
        pause's site, and a contributor leaving the group afterwards does not
        move it. The site keeps a thread paused somewhere else off that channel.
        A contributor whose own move failed while the owner's action was also
        paused would otherwise consume the owner's decision: it would take an
        ABORT_METHOD its own site does not honour, fail itself on it, and leave
        the owner waiting on a decision that had already been eaten.

        A stop ends the wait as ABORT_THREAD, as a stop on an error-paused thread
        does: one requested while the action was still running reaches the pause too.
        """
        aborts_on_stop = asyncio.create_task(self._abort_on_stop())
        try:
            decision = await self._recovery_decision_from_its_channel()
        finally:
            aborts_on_stop.cancel()
        # An operator's decision that beat the watcher must not outlive the stop.
        return RecoveryDecision.ABORT_THREAD if self._stop_event.is_set() else decision

    async def _abort_on_stop(self) -> None:
        await self._stop_event.wait()
        self._deliver_decision(RecoveryDecision.ABORT_THREAD)

    async def _recovery_decision_from_its_channel(self) -> RecoveryDecision:
        group = self._decided_by_group
        if group is not None:
            await group.decision_ready.wait()
            self._decided_by_group = None
            decision = group.decision
            assert decision is not None
            # Consume: clear paused + decision so a RETRY/RETRY_ACTION re-drive is not seen as
            # still-paused (a second recovery would be lost) and awaits a fresh decision.
            group.reset_decision()
            group.action_paused.clear()
            return decision
        await self._resume_event.wait()
        decision = self._recovery_decision
        assert decision is not None
        return decision


    def _check_abort_thread(
        self, decision: RecoveryDecision, error: BaseException,
    ) -> None:
        """Raise the ``_ThreadAbortedSignal`` sentinel if the operator
        chose ABORT_THREAD; otherwise no-op.

        Centralizes the "operator wants this thread terminated cleanly"
        translation across every site that calls :meth:`_pause_for_error`
        and would otherwise re-raise the original failure for non-RETRY
        decisions. The sentinel propagates up to :meth:`start` which
        transitions the thread to ``LabwareThreadStatus.ABORTED`` and
        returns -- so the workflow sees a terminal-but-non-failure
        thread instead of the hang-then-FAILED outcome that re-raising
        the original error would produce. Used by the action-resolution,
        move-failure, wait-step timeout, branch-step timeout, and auto-spawn
        capacity-exceeded paths.
        """
        if decision == RecoveryDecision.ABORT_THREAD:
            raise _ThreadAbortedSignal(str(error))

    async def hold_if_pause_requested(self, message: str | None = None) -> bool:
        """Hold a running action body where the operator already asked it to stop.

        Between two instrument commands, or at a manual step, nothing is in
        motion. Carrying on from there into the rest of the body would run it
        after the operator asked the execution to stop. True when the body
        carries on here.
        """
        if not self._pause_request_event.is_set():
            return True
        if self.status is not LabwareThreadStatus.EXECUTING_ACTION:
            return False
        await self._handle_manual_pause(
            resume_event=ThreadEvent.ACTION_BODY_RESUMED,
            message=message or (
                "held at a confirmed manual step because the execution is "
                "paused; resume to run the rest of the action"
            ),
            resuming_the_same_action=True,
        )
        return self.status is LabwareThreadStatus.EXECUTING_ACTION

    async def _handle_manual_pause(
        self,
        resume_event: ThreadEvent = ThreadEvent.RESUME_REQUESTED,
        message: str | None = None,
        resuming_the_same_action: bool = False,
    ) -> None:
        """Pause at a safe point and wait for resume, or for a stop.

        Used by boundary checkpoints (between methods, between actions), the
        AWAITING_CO_THREADS wait loop, and the holds inside an action body.
        ``resume_event`` says where the thread goes next: the
        boundary default re-resolves the action location, while a held action
        body returns to EXECUTING_ACTION because it never left its device.
        Racing the stop event keeps a stop from having to wait out a resume
        that is never coming; the caller's own next stop check honors it.

        A pause has no end date, so the held device goes back and is
        re-acquired on resume; holding it locks out every sibling that needs it,
        for no work. The exception is a thread ``resuming_the_same_action``: it
        goes back into the rest of an action body it never left. The release is
        before the stop check because a stopping thread has even less use for
        the device.
        """
        if not resuming_the_same_action:
            self._holdover.release_current()
        if self._stop_event.is_set():
            self._pause_request_event.clear()
            return
        self._resume_event.clear()
        self._pause_message = message
        self._fire(ThreadEvent.PAUSE_REQUESTED)
        orca_logger.info("Thread %s manually paused", self.name)
        waiters = [
            asyncio.ensure_future(self._resume_event.wait()),
            asyncio.ensure_future(self._stop_event.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await drain_cancelled_waiters(*waiters)
        self._pause_message = None
        self._pause_request_event.clear()
        if self.status is not LabwareThreadStatus.PAUSED:
            orca_logger.info("Thread %s left its pause to stop", self.name)
            return
        self._fire(resume_event)
        orca_logger.info("Thread %s resumed from manual pause", self.name)

    def _effective_failure_policy(self) -> FailurePolicy:
        """Pick the failure policy to apply to a non-action error path.

        Action error handling reads policy directly off the current action via
        `_assigned_method.current_failure_policy`, since an action is always
        executing at that point. The other error paths (move, wait-step
        timeout, generator consumption) can fire when no action is currently
        unresolved, or when no method is assigned at all (e.g. exit moves in
        `_handle_thread_completion`); both cases fall back to PAUSE so the
        operator-resume path remains the default production contract.
        """
        if self._assigned_method is None:
            return FailurePolicy.PAUSE
        try:
            return self._assigned_method.current_failure_policy
        except ValueError:
            return FailurePolicy.PAUSE

    @staticmethod
    def _pauses_under_any_policy(error: BaseException) -> bool:
        """Whether the error pauses the thread even under ``FailurePolicy.ABORT``.

        Every policy-ABORT branch (action body, device call, move, thread step,
        wait-step timeout) asks here. See ``error_policy_overrides``.
        """
        return isinstance(error, PausesUnderAnyPolicyError)

    @staticmethod
    def _should_force_pause(error: BaseException) -> bool:
        """True when the error is an external coordination signal: nothing failed, so no failure is recorded."""
        return isinstance(error, OverrideWithPauseError)

    async def _handle_action_error(self, error: Exception) -> None:
        """Handle an action failure based on the current method's failure policy.

        ABORT policy: delegates reservation release to the method and re-raises,
        unless the error is a ``PausesUnderAnyPolicyError``: a device that went
        offline, or an external coordination signal (gateway control, a
        maintenance window, operator HALT). Those pause whatever the method
        declared. See ``error_policy_overrides``.

        PAUSE policy: pauses the thread, waits for an operator decision, then
        delegates the full recovery lifecycle to ExecutingMethod.handle_recovery().

        RETRY_ACTION behavior: ``ExecutingMethod.handle_recovery(RETRY_ACTION)``
        re-runs the action body from the top. The thread's reservation is held
        across the PAUSE-RETRY cycle; the operator decides via SKIP or
        ABORT_THREAD if the reservation should be released instead. For
        ``OverrideWithPauseError`` against a still-held flag (gateway
        still controlling the device, etc.), RETRY_ACTION raises the same signal
        and re-pauses -- operator-paced loop, no auto-backoff.

        ABORT_THREAD is operator-initiated; ``_check_abort_thread``
        raises the ``_ThreadAbortedSignal`` sentinel that ``start`` catches
        and translates to ``LabwareThreadStatus.ABORTED``. Re-raising the
        original action error would land the execution at FAILED and leave
        the thread frozen in the transient ``RESOLVING_ACTION_LOCATION`` state
        set just below.
        """
        assert self._assigned_method is not None
        assert self._assigned_action is not None

        if isinstance(error, OperationDecisionSignal):
            # The op-recovery seam already paused + recorded this; apply the
            # action-level decision without re-pausing (RETRY never reaches here).
            self._fire(_recovery_event_for(error.decision))
            if error.decision == RecoveryDecision.CONTINUE:
                self._record_action_continued(error.original_error, error.unsaved)
            await self._assigned_method.handle_recovery(error.decision)
            self._assigned_action = None
            self._check_abort_thread(error.decision, error.original_error)
            return

        policy = self._assigned_method.current_failure_policy

        if policy == FailurePolicy.ABORT and not self._pauses_under_any_policy(error):
            self._failed_under_abort_policy = True
            await self._assigned_method.handle_recovery(RecoveryDecision.ABORT_THREAD)
            self._assigned_action = None
            raise error

        generation = self._assigned_method.recovery_generation

        message = f"action failed with {type(error).__name__}: {error}"
        # Skip the incident for an OverrideWithPauseError: the action body
        # did not fail, an external coordination signal (gateway control,
        # maintenance window, operator HALT) preempted it. Recording
        # ACTION_FAILED there would spam a failure record for every gateway
        # command that races a workflow. The thread still PAUSES either way.
        if not self._should_force_pause(error):
            self._record_action_failure(error, None)
        group = self._shared_action_group()
        if group is not None:
            group.mark_paused()
        decision = await self._pause_for_error(
            error, message, PauseSite.ACTION_BODY,
        )
        self._fire(_recovery_event_for(decision))
        if decision == RecoveryDecision.CONTINUE:
            self._record_action_continued(error)

        await self._assigned_method.handle_recovery(decision, generation=generation)

        self._assigned_action = None
        self._check_abort_thread(decision, error)


    async def _handle_thread_completion(self) -> None:
        # Cooperative stop during the final action: finalize STOPPED from
        # STOPPING without the end-move (same contract as _ThreadStopSignal).
        if self._stop_event.is_set():
            self._handle_thread_stop()
            return
        # Armed before the end move, not after: a successor standing on this
        # thread's end location makes that move never finish.
        self._holdover.release_current_when_drained()
        # End candidates were resolved at build: flat sites or system nodes;
        # route to all of them, the first grant wins (a hotel's shelves).
        end_locations = self._thread.end_locations
        while self.current_location not in end_locations:
            if self._pause_request_event.is_set():
                await self._handle_manual_pause()
                if self._stop_event.is_set():
                    self._handle_thread_stop()
                    return
            await self._fire_and_execute_move_to(
                ThreadEvent.MOVE_TO_END_REQUESTED,
                end_locations,
            )

        # Labware has arrived at its end_location: it exits the workflow
        # here. Dispatch is flag-based on the thread template:
        #
        #   - `template is None` -> select_end_spawn_action returns None;
        #     synthetic / template-less threads skip dispose. This avoids
        #     parking a template-less thread at AWAITING_MANUAL_REMOVE
        #     under LIVE waiting for an operator who has no
        #     author-declared contract (review item L4).
        #   - `end_leave_in_place` -> skip dispose entirely (deck-resident
        #     reagent labware that survives across executions for the
        #     reuse-bind path to re-attach), unless this receiver ended
        #     because its labware was used up: a spent rack left standing
        #     is what the next receiver would adopt.
        #   - `end_manual_remove` (bare-string default or explicit
        #     MANUAL_REMOVE) -> ManualRemoveSpawn.dispose: sim modes call
        #     `dispose_labware`; LIVE
        #     polls `end_location.labware` waiting for
        #     `labware_discharge` to clear the slot. The thread parks at
        #     `AWAITING_MANUAL_REMOVE` for the duration of the wait so
        #     operators see the specific slot via the ThreadSnapshot
        #     `waiting_for` field.
        end_spawn = select_end_spawn_action(
            self._thread, self.current_location, spent=self._ended_spent,
        )
        if end_spawn is not None:
            if self._thread.run_mode is WorkflowRunMode.LIVE:
                self._fire(ThreadEvent.LIVE_MANUAL_REMOVE_AWAITED)
            await end_spawn.dispose(self._thread)

        # `ManualRemoveSpawn._dispose_live` cooperates with `stop_event`
        # (L1 fix). When the operator cancels the thread mid-wait, the
        # dispose returns early without clearing the slot -- the labware
        # is still physically on `end_location`. Branching here on the
        # stop event keeps the registry/status truthful: a stopped
        # thread must NOT be marked ENDED (labware is still present)
        # and must NOT drain queued items to a phantom receiver. This
        # mirrors the start-side stop check at line 716 that handles
        # an `_acquire_live` cooperative-stop exit.
        if self._stop_event.is_set():
            self._handle_thread_stop()
            return

        if end_spawn is not None:
            # The labware leaves the lab: its slot, its position and the device's deck let it go, and the system
            # stops listing it. In LIVE the operator's discharge already did the last part.
            await self._require_pipeline().placer.left(self._thread.labware, self.current_location)

        # Update registry state and route any undelivered items to a fresh receiver
        if self._labware_registry is not None:
            self._labware_registry.update_state(self._thread.labware.id, LabwareState.ENDED)
            slot = self._labware_registry.get_slot(self._my_slot_key())
            if slot is not None:
                await slot.drain_for_handoff(
                    self._auto_spawn_callback, self._thread.run_mode,
                    completing_thread=self._thread,
                )

        # After the dispose, not before it: a thread whose end location is a
        # site on the device it last held still has its plate there until the
        # dispose lifts it, and the drain cannot pass while it does.
        self._holdover.settle_drains_on_departure()
        self._fire(ThreadEvent.THREAD_COMPLETED)

    async def _execute_move_action(self) -> bool:
        """Carry out the resolved move. False when an operator moved the
        labware and the caller must plan again from where it now is."""
        assert self._move_action is not None
        self._fire(ThreadEvent.MOVE_TARGET_AWAITED)
        if not await self._await_move_target():
            return False
        if self._move_action.reservation.deadlocked.is_set():
            if not await self._handle_deadlock():
                return False
        self._fire(ThreadEvent.MOVE_TARGET_GRANTED)
        context = ThreadExecutionContext(execution_id=self._context.execution_id,
                                        workflow_name=self._context.workflow_name,
                                        thread_id=self._thread.id,
                                        thread_name=self._thread.name,
                                        template_name=self._thread.template_name,
                                        group_id=self._thread.group_id)

        while True:
            if self._plan_is_stale():
                return False
            executable_move_action = self._move_action.executable(
                self._status_manager, context,
                self._labware_location_service, self._require_pipeline(),
                self._move_handler,
                pause_checkpoint=self,
            )
            try:
                await executable_move_action.execute()
                break
            except Exception as error:
                if (
                    self._effective_failure_policy() == FailurePolicy.ABORT
                    and not self._pauses_under_any_policy(error)
                ):
                    self._move_action.reservation.release_reservation()
                    self._move_action = None
                    self._failed_under_abort_policy = True
                    raise error
                message = f"move failed with {type(error).__name__}: {error}"
                # No incident when a coordination signal (gateway control, HALT)
                # preempted the move: it did not fail. Mirrors the action guard.
                if not self._should_force_pause(error):
                    self._record_move_failure(error)
                decision = await self._pause_for_error(
                    error, message, PauseSite.MOVE,
                )
                self._fire(ThreadEvent.MOVE_RETRY)
                if decision == RecoveryDecision.RETRY:
                    continue
                if decision == RecoveryDecision.CONTINUE:
                    # The surface already verified the labware is at the target,
                    # so the next pass finds nothing left to actuate.
                    self._record_move_continued(error)
                    continue
                self._move_action.reservation.release_reservation()
                self._move_action = None
                self._check_abort_thread(decision, error)
                raise error

        self._holdover.release_after_move_if_drained()
        self._move_action = None
        return True

    async def _await_move_target(self) -> bool:
        """Wait for the move's target to clear. False once the plan no longer
        starts where the labware is, or the thread was stopped or asked to pause.

        The wait races a wake-up, because the labware landing on this very
        target is one of the things an operator can do: waiting for a position
        to be vacated by the plate that belongs on it never ends. A wake that
        leaves the plan good keeps waiting.
        """
        assert self._move_action is not None
        while True:
            self._labware_moved_event.clear()
            if self._plan_is_stale():
                return False
            target_cleared = asyncio.ensure_future(self._move_action.target.wait_until_available())
            waiters = [
                target_cleared,
                asyncio.ensure_future(self._labware_moved_event.wait()),
                asyncio.ensure_future(self._stop_event.wait()),
                asyncio.ensure_future(self._pause_request_event.wait()),
            ]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                await drain_cancelled_waiters(*waiters)
            if self._stop_event.is_set() or self._pause_request_event.is_set() or self._plan_is_stale():
                return False
            if target_cleared.done() and not target_cleared.cancelled():
                return True

    async def _handle_deadlock(self) -> bool:
        """Reroute a deadlocked move to a parking spot. False when the thread is stopped, the
        plan went stale or a pause arrived while waiting for one, which can otherwise be hours."""
        assert self._move_action is not None
        orca_logger.info(f"Thread {self._thread.name} - Deadlock detected")
        old_target = self._move_action.target
        previous_location = self._location_history.get_previous_location()
        try:
            self._move_action = await self._move_handler.handle_deadlock(
                self._thread.id, self._move_action, previous_location,
                abandon_when=self._withdraw_move_when(None),
            )
        except MoveAbandonedError:
            return False
        orca_logger.info(f"Thread {self._thread.name} - Deadlock resolved - reroute target {old_target.name} to {self._move_action.target.name}")
        return True

    # === IThreadContext implementation ===
    # ``labware_template`` and ``thread_instance`` are defined earlier.

    @property
    def thread_id(self) -> str:
        return self._thread.id

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    @property
    def shared_executing_method(self) -> ExecutingMethod | None:
        return self._thread.shared_executing_method

    @property
    def variable_store(self) -> IVariableResolver:
        return self._variable_store

    @property
    def submission_id(self) -> str | None:
        return self._thread.submission_id

    @property
    def group_id(self) -> str | None:
        return self._thread.group_id

    @property
    def execution_context(self) -> WorkflowExecutionContext:
        return self._context

    @property
    def event_emitter(self) -> EventEmitter | None:
        return self._status_manager.emit_event

    @property
    def register_method_template(self) -> Callable[[object], None] | None:
        if self._register_method_template is None:
            return None
        register = self._register_method_template
        workflow_name = self._context.workflow_name
        def _adapt(template: object) -> None:
            assert isinstance(template, MethodTemplate)
            register(workflow_name, template)
        return _adapt

    def my_slot_key(self) -> str:
        return self._my_slot_key()

    def bind_method(self, method: IMethod) -> None:
        if self._thread.labware_template is None:
            return
        method.assign_thread(self._thread.labware_template, self._thread)
        self._bound_to_bring_labware_to = method

    def my_slot(self) -> IMySlotView | None:
        if self._labware_registry is None:
            return None
        slot = self._labware_registry.get_slot(self._my_slot_key())
        if slot is None:
            return None
        return _BoundSlotView(
            slot, self,
            on_empty_wait=lambda: self._fire(ThreadEvent.CO_LABWARE_AWAITED),
        )

    def mark_my_labware_parked(self) -> None:
        if self._labware_registry is None:
            return
        self._labware_registry.update_state(self._thread.labware.id, LabwareState.PARKED)

    def add_method(self, method: IMethod) -> None:
        assert self._method_registry is not None, (
            "method_registry is required for IThreadContext usage; "
            "pass it to ExecutingLabwareThread.__init__ via ExecutingThreadFactory."
        )
        self._method_registry.add_method(method)

    def create_executing_method(self, method: IMethod) -> ExecutingMethod:
        assert self._executing_method_registry_field is not None, (
            "executing_method_registry is required for IThreadContext usage; "
            "pass it to ExecutingLabwareThread.__init__ via ExecutingThreadFactory."
        )
        return self._executing_method_registry_field.create_executing_method(method.id, self._context)

    def release_holdover(self) -> None:
        self._holdover.release_current()

    async def _vacate_for_acquisition_yield(self) -> None:
        """Yield for a deadlock-flagged action acquisition (rule-7 amendment):
        park this thread's plate to a resolution pad so the drain it defers can
        complete, then re-resolve. A resident cannot move, a thread at a pad has
        nothing to vacate, and a plate may have no pad it can reach. Each says so
        and backs off, so the detector picks another thread or none.
        """
        template = self._thread.thread_template
        system_map = self._move_handler.system_map
        current = self.current_location
        immobile = template is not None and (
            template.start_reuse_existing or template.immovable
        )
        if immobile or system_map.is_deadlock_resolution_location(current.position_id):
            self._move_handler.note_cannot_yield(self._thread.id)
            await asyncio.sleep(0.5)
            return
        paths = system_map.get_shortest_paths_to_deadlock_resolution(current.position_id)
        pad_ids = {path[-1] for path in paths if len(path) > 1}
        if not pad_ids:
            self._move_handler.note_cannot_yield(self._thread.id)
            await asyncio.sleep(0.5)
            return
        pads = [self._move_handler.get_location(pid) for pid in pad_ids]
        orca_logger.info(
            f"Thread {self._thread.name} - Acquisition yield: vacating "
            f"{current.name} to a resolution pad"
        )
        # A yield-vacate is not progress: arriving at the pad must not clear
        # the thread's starvation debt or pad cooldown (escape_on_arrival=False).
        await self._fire_and_execute_move_to(
            ThreadEvent.MOVE_RESERVATION_REQUESTED, pads, escape_on_arrival=False
        )

    def location(self, name: str) -> Location:
        return self._move_handler.resolve_journey_location(name)

    async def fire_and_execute_move_to(
        self, event: ThreadEvent, target: Location | list[Location],
        abandon_when: Callable[[], bool] | None = None,
    ) -> None:
        targets = [target] if isinstance(target, Location) else list(target)
        await self._fire_and_execute_move_to(
            event, targets, abandon_when=abandon_when,
        )

    async def _hand_the_slot_on(self) -> None:
        """Give a full or used-up receiver's slot to the next receiver, if the slot hands off when full.

        This receiver goes on with its later steps; whatever overflowed past it is routed now.
        """
        self._stopped_taking_contributions = False
        if self._labware_registry is None:
            return
        slot = self._labware_registry.get_slot(self._my_slot_key())
        if slot is None or slot.active_thread is not self or not slot.hands_off_when_full():
            return
        self._ended_spent = slot.receiver_spent
        self._handed_the_slot_on = True
        await slot.drain_for_handoff(
            self._auto_spawn_callback, self._thread.run_mode, completing_thread=self._thread,
        )

    async def _yield_adapter(
        self, registry: EventChannelRegistry,
    ) -> AsyncGenerator[ExecutingMethod, None]:
        """Dispatch templates yielded by the user generator to their
        own ``IMethodTemplate.schedule(ctx, registry)`` bodies.

        Owned by the thread (not the factory) so the adapter closes
        over ``self`` directly. A ``stop_event`` check between
        iterations honors a mid-flight ``thread.stop()`` before the
        next yield. End-of-generator marks the thread's slot drained
        so auto-spawn callbacks do not bind methods to a leaving
        receiver (identity-guarded to avoid stomping on a fresh
        receiver after a handoff).
        """
        yield_func = self._thread.yield_func
        assert yield_func is not None, (
            f"Thread '{self._thread.name}' has no yield_func. "
            "ThreadFactory should always set one (static lists become generators)."
        )

        def _has_more_work() -> bool:
            if self._labware_registry is None:
                return True
            slot = self._labware_registry.get_slot(self._my_slot_key())
            if slot is None:
                return True
            # After a hand-off the slot's queue holds the next receiver's work, not this one's.
            if self._handed_the_slot_on:
                return False
            if not slot.queue.empty():
                return True
            if slot.is_closed:
                return False
            # Depletion-stop: this receiver's labware is used up, so waiting for
            # work it cannot serve strands whatever overflowed past it.
            # Capacity-stop: a full receiver finalizes now instead of looping for
            # a contribution the policy will overflow into a fresh receiver.
            if slot.receiver_spent or not slot.has_room():
                self._stopped_taking_contributions = True
                return False
            return True

        user_ctx = ThreadContext(
            registry, self._variable_store, self._context.execution_id,
            partner_constraint_setter=self.set_partner_constraint,
            labware=self._thread.labware,
            has_more_work_fn=_has_more_work,
            submission_id=self._thread.submission_id,
            event_emitter=self._status_manager.emit_event,
            workflow_name=self._context.workflow_name,
            thread_id=self._thread.id,
        )
        async for item in yield_func(user_ctx):
            if self._stop_event.is_set():
                break
            if self._stopped_taking_contributions:
                await self._hand_the_slot_on()
            templates = item if isinstance(item, list) else [item]
            for template in templates:
                if self._stop_event.is_set():
                    break
                if isinstance(template, ActionTemplate):
                    async for em in schedule_action_template(template, self):
                        yield em
                elif isinstance(template, IMethodTemplate):
                    async for em in template.schedule(self, registry):
                        yield em
                else:
                    raise TypeError(f"Yield adapter: unknown template type {type(template)}")

        # A stop-break is not a natural drain: leaving the flag unset keeps a
        # stopped receiver quarantine-eligible (drained excludes quarantine).
        if self._stop_event.is_set():
            return
        if self._labware_registry is not None:
            slot = self._labware_registry.get_slot(self._my_slot_key())
            if slot is not None and slot.active_thread is self:
                slot.receiver_drained = True
                # Read while this thread still owns the slot: a successor can
                # claim it the moment drained goes True and reset the flag.
                self._ended_spent = slot.receiver_spent


class _BoundSlotView:
    """``IMySlotView`` bound to the calling thread.

    Injects the caller's identity into ``await_next_method`` so the slot
    records WHO is waiting (``LabwareSlot.awaiting_threads``). Templates only
    ever receive this via ``ctx.my_slot()``, so the recorded identity is
    structural: a template cannot wait on another thread's behalf.

    ``on_empty_wait`` publishes the wait as thread status (the thread wires it
    to fire ``ThreadEvent.CO_LABWARE_AWAITED``), gated like the slot's
    awaiting-join record: only an empty-handed entry on a slot that is not
    already closed-and-drained is a wait. Fired BEFORE delegating so the
    status never lags the recorded fact.
    """

    def __init__(
        self, slot: LabwareSlot, waiter: IRegisteredThread,
        on_empty_wait: Callable[[], None] | None = None,
    ) -> None:
        self._slot = slot
        self._waiter = waiter
        self._on_empty_wait = on_empty_wait

    @property
    def is_closed(self) -> bool:
        return self._slot.is_closed

    def queue_empty(self) -> bool:
        return self._slot.queue_empty()

    async def await_next_method(
        self, stop_event: asyncio.Event,
    ) -> ExecutingMethod | None:
        if (
            self._on_empty_wait is not None
            and self._slot.queue_empty()
            and not self._slot.is_closed
        ):
            self._on_empty_wait()
        return await self._slot.await_next_method(stop_event, waiter=self._waiter)


class ExecutingThreadFactory:
    # Class-level default so test code that bypasses __init__ via __new__()
    # observes None rather than AttributeError.
    _residency_check: ResidencyCheck | None = None
    _status_recorder: IThreadStatusRecorder | None = None

    def __init__(self,
                 event_bus: IEventBus,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                 actions_resolver: DynamicResourceActionResolver,
                 executing_method_registry: ExecutingMethodRegistry,
                 labware_location_service: ILabwareLocationService,
                 coordination_config: CoordinationConfig | None = None,
                 method_registry: IMethodRegistry | None = None,
                 variable_store: IVariableResolver | None = None,
                 register_method_template: Callable[[str, MethodTemplate], None] | None = None,
                 residency_check: ResidencyCheck | None = None,
                 ) -> None:
        self._event_bus = event_bus
        self._actions_resolver = actions_resolver
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._executing_method_registry = executing_method_registry
        self._labware_location_service = labware_location_service
        self._coordination_config = coordination_config
        self._method_registry = method_registry
        self._variable_store = variable_store
        self._register_method_template = register_method_template
        self._residency_check = residency_check
        # Late-bound. The factory is constructed during
        # SystemBuild (SdkToSystemBuilder), BEFORE SystemRuntime exists.
        # SystemRuntime.__init__ calls set_thread_incident_declarer so every
        # thread minted after construction (i.e., during execution) gets
        # the back-ref. Threads minted before set_thread_incident_declarer fires
        # would get None -- there are none in practice (threads spawn only
        # during execution), but the optional-None type makes test fixtures
        # that bypass SystemRuntime trivial.
        self._thread_incident_declarer: IThreadIncidentDeclarer | None = None
        self._status_recorder: IThreadStatusRecorder | None = None
        self._command_pipeline: SystemCommandPipeline | None = None

    def set_command_pipeline(self, pipeline: SystemCommandPipeline) -> None:
        """Set the system's command pipeline (late-bound by System); every thread minted after this moves and places through it."""
        self._command_pipeline = pipeline

    def set_thread_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Set the post-construction back-reference for thread-incident recording.

        Called by SystemRuntime.__init__ once it has built its
        IncidentDeclarer. Every
        thread created after this returns will receive the back-ref.
        """
        self._thread_incident_declarer = declarer

    def set_thread_status_recorder(self, recorder: IThreadStatusRecorder) -> None:
        self._status_recorder = recorder

    def create_instance(self,
                        instance: LabwareThreadInstance,
                        context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        # All threads use the yield adapter on ExecutingLabwareThread
        # itself (static method lists become generators in ThreadFactory).
        assert instance.yield_func is not None, (
            f"Thread '{instance.name}' has no yield_func. "
            "ThreadFactory should always set one (static lists become generators)."
        )
        return ExecutingLabwareThread(
            instance,
            self._event_bus,
            self._move_handler,
            self._status_manager,
            self._actions_resolver,
            context,
            self._labware_location_service,
            coordination_config=self._coordination_config,
            thread_incident_declarer=self._thread_incident_declarer,
            method_registry=self._method_registry,
            executing_method_registry=self._executing_method_registry,
            variable_store=self._variable_store,
            register_method_template=self._register_method_template,
            command_pipeline=self._command_pipeline,
            residency_check=self._residency_check,
            status_recorder=self._status_recorder,
        )


class IExecutingThreadRegistry(ABC):

    @abstractmethod
    def create_executing_thread(self, thread_id: str, context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        raise NotImplementedError

    @abstractmethod
    def get_executing_thread(self, thread_id: str) -> ExecutingLabwareThread:
        raise NotImplementedError

    @abstractmethod
    def set_thread_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Forward the deadlock-declarer back-reference to the factory (S3 R1.5)."""
        raise NotImplementedError

    @abstractmethod
    def set_thread_status_recorder(self, recorder: IThreadStatusRecorder) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_command_pipeline(self, pipeline: SystemCommandPipeline) -> None:
        """Forward the placement chokepoint back-reference to the factory."""
        raise NotImplementedError

class ExecutingThreadRegistry(IExecutingThreadRegistry):
    def __init__(self,
                 thread_registry: ThreadRegistry,
                 executing_thread_factory: ExecutingThreadFactory
                 ) -> None:
        self._thread_registry = thread_registry
        self._factory = executing_thread_factory
        self._executing_registry: Dict[str, ExecutingLabwareThread] = {}

    @property
    def threads(self) -> List[ExecutingLabwareThread]:
        return list(self._executing_registry.values())

    def set_thread_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Forward the deadlock declarer down to the factory (S3 R1.5).

        Called by System.set_thread_incident_declarer, which is called by
        SystemRuntime.__init__ post-construction. Every thread minted
        after this returns receives the back-ref.
        """
        self._factory.set_thread_incident_declarer(declarer)

    def set_thread_status_recorder(self, recorder: IThreadStatusRecorder) -> None:
        self._factory.set_thread_status_recorder(recorder)

    def set_command_pipeline(self, pipeline: SystemCommandPipeline) -> None:
        """Forward the placement chokepoint down to the factory (late-bound by
        System after construction, like the incident declarer)."""
        self._factory.set_command_pipeline(pipeline)

    def create_executing_thread(self, thread_id: str, context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        """Wrap the instance registered under ``thread_id``, reusing a live wrapper.

        A thread id IS its labware id, and a deck resident keeps that id across
        runs, so a later run asks for an id whose wrapper is already terminal;
        handing that one back gives the run a dead thread. A live wrapper is
        still shared, because concurrent runs drawing on one resident must not
        each start it.
        """
        existing = self._executing_registry.get(thread_id)
        if existing is not None and not existing.has_completed():
            return existing
        instance = self._thread_registry.get_thread(thread_id)
        executing_thread = self._factory.create_instance(instance, context)
        self._executing_registry[thread_id] = executing_thread
        executing_thread.publish_initial_status()
        return executing_thread
        
    def get_executing_thread(self, thread_id: str) -> ExecutingLabwareThread:
        if thread_id not in self._executing_registry:
            raise ValueError(f"Thread {thread_id} has not been created yet.")
        return self._executing_registry[thread_id]