from abc import ABC, abstractmethod
import logging
import asyncio
from typing import AsyncGenerator, Awaitable, Callable, Dict, List, Optional, NoReturn

from pydantic import JsonValue

from orca.events.custom_emit import EventEmitter
from orca.events.event_bus_interface import IEventBus
from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import (
    ManualInterventionContext,
    ThreadExecutionContext,
    WorkflowExecutionContext,
)
from orca.config import CoordinationConfig
from orca.resource_models.labware import AnyLabwareTemplate, LabwareInstance, LabwareTemplate
from orca.resource_models.capacity import RecoverableCapacityExceededError
from orca.resource_models.labware_state import (
    ILabwareRegistry,
    IRegisteredThread,
    LabwareSlot,
    LabwareState,
)
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.location import Location
from orca.resource_models.tracked_lock import LockWait, current_lock_wait
from orca.system.reservation_manager.errors import (
    AcquisitionYieldRequested,
    ActionContinuedContext,
    ActionFailedContext,
    IThreadIncidentDeclarer,
    MoveAbandonedError,
    MoveContinuedContext,
    MoveFailedContext,
    UnresolvableDeadlockError,
)
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.reservation_manager.interfaces import IThreadReservationCoordinator
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver, UnresolvedLocationAction
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.actions.operation_recovery import (
    OperationDecisionSignal,
    device_op_recovery_handler,
)
from orca.workflow_models.error_policy_overrides import OverrideWithPauseError
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
from orca.workflow_models.labware_threads.location_history import LocationHistory
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
from orca.resource_models.labware_placement import LabwarePlacer
from orca.workflow_models.merge_lane import DroppedAnchorInsert, MergeLane
from orca.workflow_models.spawn_actions import (
    ManualPlaceSpawn,
    SpawnAction,
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

from orca.runtime.group_execution_context import GroupExecutionContext
from orca.runtime.run_modes import (
    WorkflowRunMode,
    current_execution_id,
    current_run_mode,
)
from orca.runtime.recoverable_timeout import recoverable_timeout_coordinator
from orca.system.interfaces import IMethodRegistry
from orca.variables.variable_store import IVariableResolver, NullVariableResolver
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflows.workflow_factories import MethodActionFactory, MethodFactory
from orca.workflow_models.workflows.workflow_registry import ExecutingMethodRegistry, IExecutingMethodRegistry, ThreadRegistry

AutoSpawnCallback = Callable[[str, ExecutingMethod, WorkflowRunMode], Awaitable[None]]
CapacityPrecheckCallback = Callable[[str, object | None, ExecutingMethod], Awaitable[None]]
"""Side-effect-free pre-check used before any spawn commits in a multi-input
action. Raises RecoverableCapacityExceededError (and emits the AWAITING_DECISION
event) if the spawn would overflow with OverflowAction.RECOVERABLE_REJECT;
otherwise returns silently. Prevents partial multi-input commits where one
template's spawn lands in its slot queue while another pauses for operator."""


async def _methods_to_generator(methods: List[ExecutingMethod]) -> AsyncGenerator[ExecutingMethod, None]:
    for method in methods:
        yield method

orca_logger = logging.getLogger("orca")


def _recovery_event_for(decision: RecoveryDecision) -> ThreadEvent:
    """The TSM event that un-parks a paused thread for an operator's decision."""
    if decision == RecoveryDecision.RETRY:
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
        f"RecoveryDecision.{decision.name} has no thread event. RETRY_OP is settled "
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
    sees a terminal-but-non-failure thread state. Bug TTT regression:
    pre-fix the original error was re-raised, leaving the thread stuck
    in ``RESOLVING_ACTION_LOCATION`` (the transient state set just
    before ``handle_recovery``) and the execution at ``failed`` instead
    of ``completed``.
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


class ExecutingLabwareThread(ILabwareThread, IThreadContext):

    # Class-level default so test code that bypasses __init__ via __new__()
    # observes None rather than AttributeError. Real instances shadow this
    # via __init__'s instance assignment.
    _capacity_precheck_callback: CapacityPrecheckCallback | None = None

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
                 labware_placer: LabwarePlacer | None = None,
                 residency_check: ResidencyCheck | None = None,
                 ) -> None:
        self._thread = thread
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._state_machine = ThreadStateMachine(initial=LabwareThreadStatus.CREATED)
        self._coordination_config = coordination_config or CoordinationConfig()
        self._context: WorkflowExecutionContext = context
        self._event_bus = event_bus
        self._action_resolver = actions_resolver
        # S3 Round 1.5: when set (by ExecutingThreadFactory, populated by
        # SystemRuntime.__init__), the typed UnresolvableDeadlockError catch
        # below auto-records IncidentCategory.UNRESOLVABLE_DEADLOCK and fans
        # out pause_all_threads so the deadlock surfaces on every operator
        # surface (orca incidents list / GET /api/incidents / incidents_list
        # MCP). Optional so tests that build threads directly stay simple.
        self._thread_incident_declarer: IThreadIncidentDeclarer | None = thread_incident_declarer
        # Yield thread: MergeLane created lazily in start() after registry is set
        self._method_lane: MergeLane[ExecutingMethod] = MergeLane(
            _methods_to_generator([]),  # placeholder, replaced in start()
            name_getter=lambda m: m.name,
        )
        self._assigned_method: ExecutingMethod | None = None
        self._event_results: dict[str, tuple[str | None, dict[str, JsonValue]]] = {}

        self._completed_methods: List[ExecutingMethod] = []
        self._labware_location_service = labware_location_service
        self._labware_placer = labware_placer


        # Only a REUSE_EXISTING bind has already claimed the slot. Calling any
        # other thread's not-yet-arrived labware present is what persisted it.
        if self._thread.start_location.labware is self._thread.labware:
            labware_location_service.update(self._thread.labware, self._thread.start_location)
        else:
            labware_location_service.expect(
                self._thread.labware,
                self._thread.start_location,
                arrival_mechanism_for(self._thread),
            )
        self._location_history = labware_location_service.get_history(self._thread.labware)
        self._holdover = ReservationHoldover(residency_check)
        self._assigned_action: ExecutableLocationAction | None = None
        self._move_action: MoveAction | None = None
        self._labware_moved_event = asyncio.Event()
        self._planned_from: Location | None = None
        self._following_peer_action = False
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
        self._partner_constraints: dict[str, dict[str, str]] = {}

        # Park/wake lifecycle
        self._labware_registry: ILabwareRegistry | None = None
        # True when this receiver's generator returned because its labware was
        # used up. Spent labware must not stay on its slot for the next
        # receiver to adopt, whatever the template's end disposition says.
        self._ended_spent: bool = False

        # Error recovery pause/resume
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # not paused initially
        self._recovery_decision: RecoveryDecision | None = None
        self._last_error: Exception | None = None
        # What the thread was doing when it stopped, in words. The exception
        # alone cannot say: "pf400_1 is not connected" fits a home or a pick.
        self._pause_message: str | None = None
        self._pause_site: PauseSite | None = None
        # The device command this thread is paused inside; None at every other
        # pause. Names the call RETRY_OP re-runs.
        self._op_pause_command: str | None = None

        # Manual pause (cooperative). The event makes the request
        # awaitable so the AWAITING_CO_THREADS wait loop can race it
        # alongside ``all_labware_is_present`` / ``exit_signal``; boundary
        # checks at the method/action seams use ``is_set()`` exactly as
        # they used the previous bool flag.
        self._pause_request_event: asyncio.Event = asyncio.Event()
        self._held_at_start = False
        # Who asked for a non-error pause: "manual" (a person) or "system"
        # (the runtime protecting itself). Overwritten on every request.
        self._pause_reason: str = "manual"

        # Cross-thread error propagation
        self._event_channel_registry: EventChannelRegistry | None = None

        # Round 5 S1-A: candidate locations the reservation retry loop is
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

    @property
    def pending_reservation_candidates(self) -> List[str]:
        """Read-only view of the candidate locations the thread is waiting on.

        Empty when the thread is not parked in
        ``AWAITING_ACTION_RESERVATION``. ``derive_waiting_for`` reads
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
        the thread isn't group/submission-tagged (pre-T6 paths).
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
        return self._labware_registry.slot_key_for(tpl, ctx)

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

    def append_method_sequence(self, method: IMethod) -> None:
        self._thread.append_method_sequence(method)

    @property
    def location_history_names(self) -> List[str]:
        return self._location_history.get_history_names()

    @property
    def method_lane(self) -> MergeLane[ExecutingMethod]:
        return self._method_lane

    @property
    def event_results(self) -> dict[str, tuple[str | None, dict[str, JsonValue]]]:
        """Event values received by WaitStep/BranchStep resolution."""
        return self._event_results

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

    def _require_placer(self) -> LabwarePlacer:
        """A move records through the chokepoint, so a thread that can move has
        to have one."""
        if self._labware_placer is None:
            raise RuntimeError(
                f"thread {self._thread.name!r} has no labware placer, so a move "
                f"has nowhere to record what it did"
            )
        return self._labware_placer

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
        # Terminal FAILED/ABORTED carry their cause too: the record sink
        # upserts every transition, and a None here would erase the stored WHY.
        if self._last_error is not None and status in (
            LabwareThreadStatus.PAUSED,
            LabwareThreadStatus.FAILED,
            LabwareThreadStatus.ABORTED,
        ):
            last_error = str(self._last_error)
        context: ThreadExecutionContext
        manual_location = self._manual_intervention_location(status)
        if manual_location is not None:
            context = ManualInterventionContext(
                execution_id=self._context.execution_id,
                workflow_name=self._context.workflow_name,
                thread_id=self._thread.id,
                thread_name=self._thread.name,
                template_name=self._thread.template_name,
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
                pause_reason=pause_reason,
                last_error=last_error,
            )
        self._status_manager.set_status("THREAD", self._thread.id, status.name, context)
        # Terminal states: fire ``completed`` so anyone awaiting the thread
        # task unblocks regardless of whether the exit was natural completion,
        # operator stop, or operator-initiated abort. Bug TTT regression:
        # pre-fix the event fired only on COMPLETED, so an ABORTED thread
        # left ``ThreadManager.async_execute``'s ``gather(*[t.completed.wait()])``
        # hanging until the upstream 60s timeout marked the execution failed.
        if status in (
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
        ):
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
        The stamp would still say the device call and offer RETRY_OP for one
        that is no longer suspended. Same reasoning as
        ``paused_device_command``, which has always read the group.
        """
        group = self._shared_action_group()
        if self._following_peer_action and group is not None and group.action_paused.is_set():
            return (
                PauseSite.DEVICE_OP if group.op_paused_command is not None
                else PauseSite.ACTION_BODY
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
        group is suspended in one. This is what RETRY_OP is judged against, so
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
                f"Use resume_from_manual_pause() to resume."
            )
        self._refuse_a_verb_this_site_does_not_honour(decision)
        if decision == RecoveryDecision.CONTINUE:
            self._refuse_continue_the_ledger_does_not_back()
        # The same channel the pause is waiting on, chosen the same way. A
        # thread paused somewhere other than the group's action -- its own move,
        # its own spawn -- waits on its own event, so submitting to the group
        # would leave it parked on a decision that had already been delivered
        # somewhere else.
        group = self._shared_action_group()
        if (
            group is not None and group.action_paused.is_set()
            and self.pause_site in (PauseSite.ACTION_BODY, PauseSite.DEVICE_OP)
        ):
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
        # RETRY_OP is refused by every site but one, and the site's own reason
        # is about actions, which would not answer the operator who sent it.
        because = (
            "it re-runs the device call the thread is suspended inside, and "
            "no call is suspended here"
            if decision is RecoveryDecision.RETRY_OP
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
        # than "all threads ran their full method sequence" (Bug TTT).
        return self.status in (
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
        )

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
        template = self._thread.thread_template
        if (
            template is not None
            and template.start_reuse_existing
            and self._thread.start_location.labware is self._thread.labware
        ):
            # Reuse-bind placed this labware before the thread existed; a spawn
            # would self-collide on the bridge and wedge the thread at CREATED.
            # A replacement receiver never reuse-bound, so it spawns normally.
            return
        spawn = select_spawn_action(
            self._thread, self._labware_location_service, self._move_handler,
        )
        if (
            isinstance(spawn, ManualPlaceSpawn)
            and self._thread.run_mode is WorkflowRunMode.LIVE
        ):
            self._fire(ThreadEvent.LIVE_MANUAL_PLACE_AWAITED)
        try:
            await spawn.acquire(self._thread)
        except BaseException:
            self._labware_location_service.stop_expecting(self._thread.labware)
            raise
        arrived = (
            self._labware_location_service.placement(self._thread.labware)
            is PlacementState.PRESENT
        )
        if not arrived and self._stop_event.is_set():
            # Cooperatively stopped mid-wait. Nothing physical exists to
            # project, and the expectation must not outlive the thread.
            self._labware_location_service.stop_expecting(self._thread.labware)
            return
        self._assert_arrived(spawn)
        # acquire wrote the slot + bridge loaded-list; project onto the LH deck
        # so a deck-site start is pickable (no-op off a liquid handler).
        # A stop that lands after the labware arrived still has to project it:
        # a dispense or a sim place does not consult the stop event, so the
        # plate is really on the slot and a driver deck that never heard about
        # it fails later as an unexplained pick.
        if self._labware_placer is not None:
            await self._labware_placer.project_devices(
                self._thread.labware, self._thread.start_location,
            )

    def _assert_arrived(self, spawn: SpawnAction) -> None:
        """A spawn that returns without a stop must have placed the labware.

        Loud on purpose. Skipping the deck projection instead would let the run
        continue with the driver deck never told about this plate, and the
        failure would surface later as an unexplained pick, far from the spawn
        that never wrote the slot.
        """
        # Deferred: runtime_interface closes a cycle back through plugins/events.
        from orca.runtime.runtime_interface import SpawnDidNotPlaceError

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
        if self.status in {
            LabwareThreadStatus.STOPPING,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.FAILED,
        }:
            return
        # An error pause watches the recovery channel and nothing else, so end
        # it the way a person would: the action gives its device back and says
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

    async def _auto_spawn_for_action(self, unresolved: UnresolvedLocationAction) -> None:
        if self._auto_spawn_callback is None:
            return
        assert self._assigned_method is not None
        # Contributors (joined via orca.join()) should not auto-spawn.
        # Only the owning thread spawns contributors for its actions.
        if self._assigned_method.shared_coord.is_contributor(self._thread.id):
            return
        assert self._thread.labware_template is not None
        my_labware_name = self._thread.labware_template.name

        # Filter to the templates this iteration would actually spawn for.
        candidates: list[LabwareTemplate] = []
        for input_template in unresolved.expected_input_templates:
            if isinstance(input_template, AnyLabwareTemplate):
                continue
            if input_template.name == my_labware_name:
                continue
            # A filled input slot already has a live thread (entry threads
            # pre-bind their labware); spawn only inputs with no thread yet.
            if unresolved.is_input_assigned(input_template):
                continue
            spawn_key = (unresolved.id, input_template.name)
            if spawn_key in self._auto_spawned:
                continue
            candidates.append(input_template)

        # Pre-check pass: for multi-input actions with RECOVERABLE_REJECT
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
                # Raises RecoverableCapacityExceededError (and emits
                # AWAITING_DECISION) if this spawn would overflow with that
                # policy. Side-effect-free for committing — no queue puts,
                # no contribution increments.
                await self._capacity_precheck_callback(
                    input_template.name, contributor_ctx, self._assigned_method,
                )

        # Commit pass: all candidates either lack a recoverable-overflow
        # policy or have room, so no partial multi-input commit can occur
        # via the recoverable path.
        for input_template in candidates:
            spawn_key = (unresolved.id, input_template.name)
            self._auto_spawned.add(spawn_key)
            constraints = self._partner_constraints.get(input_template.name)
            if constraints is not None:
                self._assigned_method.set_partner_constraints(input_template.name, constraints)
            # Stash this thread's group/submission identity on the shared
            # method so the group-aware registry can compose per-group slot
            # keys. None-safe: if this thread is pre-T6 (untagged), the
            # registry falls back to the labware_name-only key.
            if contributor_ctx is not None:
                self._assigned_method.set_contributor_context(contributor_ctx)
            try:
                await self._auto_spawn_callback(
                    input_template.name,
                    self._assigned_method,
                    self._thread.run_mode,
                )
            except RecoverableCapacityExceededError:
                # Defensive backstop: pre-check should have caught this. If
                # an active receiver completed between pre-check and commit
                # (extremely narrow window — callback chain is sync), roll
                # back so RETRY can re-attempt cleanly.
                self._auto_spawned.discard(spawn_key)
                raise

    async def _auto_spawn_with_recovery(self, unresolved: UnresolvedLocationAction) -> None:
        """Pause-on-recoverable-overflow wrapper around _auto_spawn_for_action.

        RETRY re-attempts the spawn, which succeeds if capacity freed in the
        interim (``slot.close()`` forcing a fresh receiver). ABORT_THREAD ends
        the contributor at ABORTED rather than FAILED.

        The action-level verbs never arrive: the spawn callback fires before
        action resolution, so there is no current action to recover, and
        ``HONOURED_DECISIONS`` refuses them on the call. This site used to
        refuse them itself by re-pausing, which was the right answer written in
        the wrong place -- the operator learned their verb did not apply only
        after the thread had un-paused and paused again.
        """
        while True:
            try:
                await self._auto_spawn_for_action(unresolved)
                return
            except RecoverableCapacityExceededError as error:
                decision = await self._pause_for_error(
                    error, f"capacity exceeded: {error}",
                    PauseSite.SPAWN_CAPACITY,
                )
                if decision == RecoveryDecision.RETRY:
                    self._fire(ThreadEvent.RECOVERY_RETRY)
                    continue
                self._check_abort_thread(decision, error)
                raise error

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

        try:
            await self._start_body()
        except Exception as exc:
            # The original crash is the payload the fatal path records; the
            # guarded terminal fire must never replace it.
            self._fire_thread_failed(exc)
            raise

    def _fire_thread_failed(self, cause: Exception) -> None:
        """Land an unhandled crash in terminal FAILED; no-op if already terminal."""
        if self.status in (
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
        ):
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
        """Land a hard-cancelled thread in terminal STOPPED; no-op if already terminal.

        ``abort_execution`` cancels the task, and ``CancelledError`` is a
        BaseException no handler in ``start`` catches, so without this the
        thread keeps its pre-abort status and the aborted execution goes on
        reporting it as active.
        """
        if self.status in (
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
        ):
            return
        try:
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
            await self.initialize_labware()

        if self._stop_event.is_set():
            self._handle_thread_stop()
            return

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
                # Bug TTT: operator-initiated ABORT_THREAD recovery.
                # Transition to the terminal ABORTED state so anyone awaiting
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
            except _ThreadStopSignal:
                # Co-labware race woke on stop_event. Skip end-location move and land
                # at STOPPED; contributors follow stop_event directly, so no publish.
                orca_logger.info(
                    "Thread %s - cooperative stop honored mid-action",
                    self._thread.name,
                )
                self._handle_thread_stop()
            except asyncio.CancelledError:
                # Hard abort cancels this task; stamp terminal before unwinding.
                self._fire_thread_stopped_on_cancel()
                raise
            except Exception:
                # Owner failed post-binding (move/deadlock/wait-step) without engaging the
                # group pause; release parked contributors so they abort, not strand.
                self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
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

    def _declare_unresolved_anchor_inserts(
        self, dropped: list[DroppedAnchorInsert], target_type: str
    ) -> None:
        """Surface each dropped Before/After insert as a WARNING incident.

        The drop is expected, whether the anchor never ran or the insert
        never got its turn; this only makes it loud. No-op without a declarer
        (threads minted before SystemRuntime binds itself).

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
            except Exception as error:
                # Generator-level errors (yield thread exceptions, branch no-match)
                # route through error recovery so operator can intervene under
                # PAUSE policy. Under ABORT, surface the error directly so
                # operator-less runs (sim/CI) fail loudly instead of parking.
                # ``OverrideWithPauseError`` subclasses override that: external
                # coordination signals always route to PAUSE so the operator
                # decides recovery, never the workflow author's ABORT
                # declaration. See ``error_policy_overrides`` for the taxonomy.
                if (
                    self._effective_failure_policy() == FailurePolicy.ABORT
                    and not self._should_force_pause(error)
                ):
                    raise error
                decision = await self._pause_for_error(
                    error, f"thread step failed: {type(error).__name__}: {error}",
                    PauseSite.THREAD_STEP,
                )
                if decision == RecoveryDecision.RETRY:
                    continue
                self._check_abort_thread(decision, error)
                raise error

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
                channel = self._event_channel_registry.get_or_create(wait_event_name)
                seen = 0
                while True:
                    try:
                        await channel.wait(seen_counter=seen, timeout=self._assigned_method.wait_timeout)
                        break
                    except asyncio.TimeoutError as err:
                        # ``asyncio.TimeoutError`` is the only exception type
                        # this site catches today, and it cannot inherit
                        # ``OverrideWithPauseError`` (it's a stdlib class), so
                        # the override check is unreachable today. The check
                        # is kept for symmetry with the other three policy-
                        # ABORT branches: if a future event-channel surface
                        # raises an override-aware exception through this
                        # path, the override is honored automatically.
                        if (
                            self._effective_failure_policy() == FailurePolicy.ABORT
                            and not self._should_force_pause(err)
                        ):
                            raise
                        decision = await self._pause_for_error(
                            err,
                            f"thread step failed: wait event "
                            f"{wait_event_name!r} timed out after "
                            f"{self._assigned_method.wait_timeout}s",
                            PauseSite.WAIT_EVENT_TIMEOUT,
                        )
                        if decision == RecoveryDecision.RETRY:
                            continue
                        self._check_abort_thread(decision, err)
                        raise
                self._assigned_method = None
                continue

            self._assigned_method.set_current_thread(
                self._thread.id, self._thread.name, self._thread.submission_id,
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
                        self._assigned_action = await self._assigned_method.wait_for_current_action()
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
                    # S3 Round 1.5: typed catch for unresolvable-deadlock
                    # declarations. Record the typed
                    # IncidentCategory.UNRESOLVABLE_DEADLOCK incident +
                    # fan out pause_all_threads(execution_id) BEFORE
                    # entering the per-thread pause path so the deadlock
                    # surfaces on every operator surface (orca incidents
                    # list / GET /api/incidents / incidents_list MCP) and
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
                except Exception as error:
                    # Round 5 S1: ``ActionReservationTimeoutError`` flows
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

                # Spawn contributors only after the owner holds the device; a
                # pre-reservation spawn floods the single-lane bridges (deadlock).
                await self._auto_spawn_with_recovery(unresolved)

                target_sites = self._resolve_action_target_sites(unresolved)
                self._assigned_action.action.set_site_correlation({
                    template.name: f"{self._assigned_action.action.location.position_id}/{site}"
                    for template, site in unresolved.deck_positions.items()
                })
                while self.current_location not in target_sites:
                    await self._resolve_and_execute_move(target_sites)

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
        from orca.runtime.runtime_interface import TransitLabwareMissingDeckSiteError
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
                except Exception as error:
                    if not replanned:
                        raise
                    # Someone named a position no route reaches; letting this
                    # escape would kill the thread instead of asking again.
                    await self._pause_for_unroutable_replan(error)
                    continue
                else:
                    if await self._execute_move_action():
                        return
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

    async def _pause_for_unroutable_replan(self, error: Exception) -> None:
        """Park at the move site after a re-plan that found no route.

        The operator has just said where the labware is; if nothing can reach
        it from there, the answer is another correction, so the thread waits
        for one instead of dying. RETRY plans again from whatever they say
        next. There is no move bound to declare finished, so CONTINUE is
        refused by the same check that refuses it at any other bare pause.
        """
        message = f"move re-plan failed with {type(error).__name__}: {error}"
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
        condition, or once the plan being requested no longer starts where the
        labware is."""
        if abandon_when is None:
            return self._plan_is_stale
        return lambda: self._plan_is_stale() or abandon_when()

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


    @property
    def co_labware_timeout(self) -> float | None:
        return self._coordination_config.co_labware_timeout

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
        self._fire(ThreadEvent.CO_LABWARE_AWAITED)
        self._fire(ThreadEvent.ACTION_RESOLVED)
        paused_here = False
        self._following_peer_action = True
        try:
            while not group.outcome.done():
                waiters = [asyncio.ensure_future(self._stop_event.wait())]
                if not paused_here:
                    waiters.append(asyncio.ensure_future(group.action_paused.wait()))
                try:
                    await asyncio.wait(
                        {group.outcome, *waiters}, return_when=asyncio.FIRST_COMPLETED,
                    )
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
                        PauseSite.DEVICE_OP if group.op_paused_command is not None
                        else PauseSite.ACTION_BODY
                    )
                    self._fire(ThreadEvent.ERROR_PAUSE)
                    paused_here = True
        except asyncio.CancelledError:
            # An ABORT-policy owner publishes ABORT_THREAD then re-raises, cancelling
            # this task before it reads the outcome; honor it (ABORTED) not frozen.
            if (
                group.outcome.done()
                and group.outcome.result().decision == RecoveryDecision.ABORT_THREAD
            ):
                self._assigned_action = None
                raise _ThreadAbortedSignal("shared rendezvous aborted by the owner")
            raise
        finally:
            self._following_peer_action = False
        self._assigned_action = None
        # Own cooperative stop wins over a group outcome a co-torn-down owner
        # published: a stopped contributor lands STOPPED, never spins on it.
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

    def _react_to_shared_outcome(self, outcome: ActionResolution) -> None:
        """React to the group's published outcome. ABORT_THREAD tears this contributor
        down; every other outcome returns so the method loop advances to the next action
        or exits on ``completed`` (ABORT_METHOD / completion)."""
        if outcome.decision == RecoveryDecision.ABORT_THREAD:
            raise _ThreadAbortedSignal("shared rendezvous aborted by the owner")

    def _publish_owner_slot_outcome_on_exit(self, decision: RecoveryDecision) -> None:
        """Publish a terminal action outcome so contributors fan out when the OWNER
        leaves the rendezvous outside the normal funnels (handle_recovery / completion):
        a resolution error, a co-labware timeout, or a cooperative stop. Idempotent;
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
            self._fire(ThreadEvent.CO_LABWARE_AWAITED)
            outcome = await CoLabwareCoordinator.wait(
                stop_event=self._stop_event,
                exit_event=self._assigned_method.exit_signal,
                pause_event=self._pause_request_event,
                co_labware_event=self._assigned_action.action.all_labware_is_present,
                timeout=self.co_labware_timeout,
            )

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
                # Publish ABORT_THREAD so an arrived contributor aborts cleanly rather
                # than zombie-parking; the owner itself FAILs via the TimeoutError below.
                self._publish_owner_slot_outcome_on_exit(RecoveryDecision.ABORT_THREAD)
                missing_names = ", ".join(
                    self._assigned_action.action.missing_input_report()
                ) or "unknown"
                raise TimeoutError(
                    f"Thread {self._thread.name} timed out ({self.co_labware_timeout}s) waiting "
                    f"for co-labware at {self._assigned_action.action.location.name}. "
                    f"Missing: {missing_names}"
                )

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
        try:
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
        (production ``SystemRuntime``), this records an incident under
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
        self, error: Exception, command: str,
    ) -> RecoveryDecision:
        """Operation-level recovery seam, consulted by the device dispatcher when a
        device call in this thread's action body fails. Records the failure, pauses
        the thread for an operator decision, and returns it: RETRY_OP makes the
        dispatcher re-invoke just the failed call (the body stays suspended at its
        await); an action-level decision (RETRY / ABORT_*) is applied by
        ``_handle_action_error`` via the ``OperationDecisionSignal`` the dispatcher
        then raises.

        On a shared action the owner drives the op alone, so only it pauses here; the
        decision is published to contributors as the slot's set-once outcome. Contributors
        never enter this seam (they await the outcome, they do not run the op).
        """
        assert self._assigned_method is not None
        if (
            self._assigned_method.current_failure_policy == FailurePolicy.ABORT
            and not self._should_force_pause(error)
        ):
            # ABORT policy has no operator loop; propagate for whole-action abort.
            raise error
        self._record_action_failure(error, command)
        op_context = ThreadExecutionContext(
            execution_id=self._context.execution_id,
            workflow_name=self._context.workflow_name,
            thread_id=self._thread.id,
            thread_name=self._thread.name,
            template_name=self._thread.template_name,
            last_error=str(error),
        )
        # One-shot operator signal (action stays suspended, does not ERROR).
        # emit_event not set_status: set_status' (command,"FAILED") dedup drops repeats.
        self._status_manager.emit_event(f"DEVICE_OP.{command}.FAILED", op_context)
        message = f"device op '{command}' failed with {type(error).__name__}: {error}"
        self._op_pause_command = command
        group = self._shared_action_group()
        if group is not None:
            group.mark_paused()
            group.mark_op_paused(command)
        try:
            decision = await self._pause_for_error(
                error, message, PauseSite.DEVICE_OP,
            )
        finally:
            self._op_pause_command = None
            if group is not None:
                group.clear_op_paused()
        if decision == RecoveryDecision.RETRY_OP:
            # Un-pause for op-level retry; action-level decisions (RETRY/ABORT_*)
            # un-pause in _handle_action_error after the body unwinds.
            self._fire(ThreadEvent.RECOVERY_RETRY)
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
        (production ``SystemRuntime``) it adds a queryable
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

    def _record_action_continued(self, error: BaseException) -> None:
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
        (``RETRY``, ``RETRY_OP``, ``CONTINUE``, ``ABORT_ACTION``,
        ``ABORT_METHOD``) because the thread keeps running and a later clean
        completion must not leak the recovered error onto its snapshot. Only
        ``ABORT_THREAD`` (terminal) preserves it, so ``thread.last_error`` keeps
        attributing the abort to its cause after the thread lands ``ABORTED``.
        """
        # The longest wait in the system: it ends when a person decides. The
        # two body-level sites resume into the device they are holding.
        if site not in (PauseSite.ACTION_BODY, PauseSite.DEVICE_OP):
            self._holdover.release_current()
        self._last_error = error
        self._pause_message = message
        self._pause_site = site
        self._resume_event.clear()
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

        decision = await self._await_recovery_decision(site)
        orca_logger.info("Thread %s resuming with decision: %s", self.name, decision.name)

        self._recovery_decision = None
        if decision != RecoveryDecision.ABORT_THREAD:
            self._last_error = None
            self._pause_message = None
            self._pause_site = None
        return decision

    async def _await_recovery_decision(self, site: PauseSite) -> RecoveryDecision:
        """Wait for the recovery decision, from the right channel.

        A shared action that paused as a group takes ONE decision, fed by any
        participant, and the group's pause is always at the action body or the
        device call inside it -- those are the only two sites that mark it.

        ``site`` is what keeps a thread paused somewhere else off that channel.
        A contributor whose own move failed while the owner's action was also
        paused would otherwise consume the owner's decision: it would take an
        ABORT_METHOD its own site does not honour, fail itself on it, and leave
        the owner waiting on a decision that had already been eaten.
        """
        group = self._shared_action_group()
        if (
            group is not None and group.action_paused.is_set()
            and site in (PauseSite.ACTION_BODY, PauseSite.DEVICE_OP)
        ):
            await group.decision_ready.wait()
            decision = group.decision
            assert decision is not None
            # Consume: clear paused + decision so a RETRY/RETRY_OP re-drive is not seen as
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
        the original error would produce. Bug TTT regression covers
        the action-error path; this helper extends the same fix to
        action-resolution, move-failure, wait-step timeout,
        branch-step timeout, and auto-spawn capacity-exceeded paths.
        """
        if decision == RecoveryDecision.ABORT_THREAD:
            raise _ThreadAbortedSignal(str(error))

    async def hold_if_pause_requested(self) -> None:
        """Hold a running action body where the operator already asked it to stop.

        A manual step is the safest point the thread ever occupies: nothing is
        in motion and the operator is standing at the instrument. Returning
        from their confirm straight into the rest of the body would run it
        after they asked the execution to stop.
        """
        if not self._pause_request_event.is_set():
            return
        if self.status is not LabwareThreadStatus.EXECUTING_ACTION:
            return
        await self._handle_manual_pause(
            resume_event=ThreadEvent.ACTION_BODY_RESUMED,
            message=(
                "held at a confirmed manual step because the execution is "
                "paused; resume to run the rest of the action"
            ),
            resuming_the_same_action=True,
        )

    async def _handle_manual_pause(
        self,
        resume_event: ThreadEvent = ThreadEvent.RESUME_REQUESTED,
        message: str | None = None,
        resuming_the_same_action: bool = False,
    ) -> None:
        """Pause at a safe point and wait for resume, or for a stop.

        Used by boundary checkpoints (between methods, between actions), the
        AWAITING_CO_THREADS wait loop, and a confirmed manual step inside an
        action body. ``resume_event`` says where the thread goes next: the
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
    def _should_force_pause(error: BaseException) -> bool:
        """True when the error is an external coordination signal that
        must override a declared ``FailurePolicy.ABORT`` and route to PAUSE.

        See ``orca.workflow_models.error_policy_overrides`` for the marker
        base. The check is centralized here so every policy-ABORT branch
        site (action body, move action, generator consumption, wait-step
        timeout) applies the override identically.
        """
        return isinstance(error, OverrideWithPauseError)

    async def _handle_action_error(self, error: Exception) -> None:
        """Handle an action failure based on the current method's failure policy.

        ABORT policy: delegates reservation release to the method and re-raises,
        unless the error is an ``OverrideWithPauseError`` subclass. External
        coordination signals (gateway control, maintenance windows, operator
        HALT) route through PAUSE regardless of the declared policy: the
        method itself did not fail, so the workflow author's ABORT intent
        does not apply. See ``error_policy_overrides`` for the full
        taxonomy.

        PAUSE policy: pauses the thread, waits for an operator decision, then
        delegates the full recovery lifecycle to ExecutingMethod.handle_recovery().

        RETRY behavior: ``ExecutingMethod.handle_recovery(RETRY)`` re-runs
        the action body from the top. The thread's reservation is held
        across the PAUSE-RETRY cycle; the operator decides via SKIP or
        ABORT_THREAD if the reservation should be released instead. For
        ``OverrideWithPauseError`` against a still-held flag (gateway
        still controlling the device, etc.), RETRY raises the same signal
        and re-pauses -- operator-paced loop, no auto-backoff.

        Bug TTT: ABORT_THREAD is operator-initiated; ``_check_abort_thread``
        raises the ``_ThreadAbortedSignal`` sentinel that ``start`` catches
        and translates to ``LabwareThreadStatus.ABORTED``. Re-raising the
        original action error (the pre-fix path) landed the execution at
        FAILED and left the thread frozen in the transient
        ``RESOLVING_ACTION_LOCATION`` state set just below.
        """
        assert self._assigned_method is not None
        assert self._assigned_action is not None

        if isinstance(error, OperationDecisionSignal):
            # The op-recovery seam already paused + recorded this; apply the
            # action-level decision without re-pausing (RETRY_OP never reaches here).
            self._fire(_recovery_event_for(error.decision))
            if error.decision == RecoveryDecision.CONTINUE:
                self._record_action_continued(error.original_error)
            await self._assigned_method.handle_recovery(error.decision)
            self._assigned_action = None
            self._check_abort_thread(error.decision, error.original_error)
            return

        policy = self._assigned_method.current_failure_policy

        if policy == FailurePolicy.ABORT and not self._should_force_pause(error):
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
        #     `dispose_labware` exactly as the legacy branch did; LIVE
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
            # The dispose released the labware from the world; retire its
            # position so a reboot does not resurrect it at the end slot.
            self._labware_location_service.retire(self._thread.labware)
            if self._labware_placer is not None:
                # The dispose clears the site but says nothing to the driver.
                # A deck site left holding it materialized collides with
                # whatever the next receiver projects onto the same slot.
                await self._labware_placer.project_devices(
                    self._thread.labware, self.current_location,
                )

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
                                        template_name=self._thread.template_name)

        while True:
            if self._plan_is_stale():
                return False
            executable_move_action = self._move_action.executable(
                self._status_manager, context,
                self._labware_location_service, self._require_placer(),
                self._move_handler,
            )
            try:
                await executable_move_action.execute()
                break
            except Exception as error:
                if (
                    self._effective_failure_policy() == FailurePolicy.ABORT
                    and not self._should_force_pause(error)
                ):
                    self._move_action.reservation.release_reservation()
                    self._move_action = None
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
        starts where the labware is.

        The wait races a wake-up, because the labware landing on this very
        target is one of the things an operator can do: waiting for a position
        to be vacated by the plate that belongs on it never ends.
        """
        assert self._move_action is not None
        self._labware_moved_event.clear()
        if self._plan_is_stale():
            return False
        waiters = [
            asyncio.ensure_future(self._move_action.target.wait_until_available()),
            asyncio.ensure_future(self._labware_moved_event.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await drain_cancelled_waiters(*waiters)
        return not self._plan_is_stale()

    async def _handle_deadlock(self) -> bool:
        """Reroute a deadlocked move to a parking spot. False when the plan
        went stale while waiting for one, which can otherwise be hours."""
        assert self._move_action is not None
        orca_logger.info(f"Thread {self._thread.name} - Deadlock detected")
        old_target = self._move_action.target
        previous_location = self._location_history.get_previous_location()
        try:
            self._move_action = await self._move_handler.handle_deadlock(
                self._thread.id, self._move_action, previous_location,
                abandon_when=self._plan_is_stale,
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
        complete, then re-resolve. Residents cannot move and threads already at
        a pad have nothing to vacate; both back off briefly instead -- R1's
        starvation ratchet rotates the flag to the thread whose park helps.
        """
        template = self._thread.thread_template
        system_map = self._move_handler.system_map
        current = self.current_location
        immobile = template is not None and (
            template.start_reuse_existing or template.immovable
        )
        if immobile or system_map.is_deadlock_resolution_location(current.position_id):
            await asyncio.sleep(0.5)
            return
        paths = system_map.get_shortest_paths_to_deadlock_resolution(current.position_id)
        pad_ids = {path[-1] for path in paths if len(path) > 1}
        if not pad_ids:
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
            if not slot.queue.empty():
                return True
            if slot.is_closed:
                return False
            # Depletion-stop: this receiver's labware is used up, so waiting for
            # work it cannot serve strands whatever overflowed past it.
            if slot.receiver_spent:
                return False
            # Capacity-stop: a full receiver finalizes now instead of looping for
            # a contribution the policy will overflow into a fresh receiver.
            return slot.has_room()

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

    def __init__(self,
                 event_bus: IEventBus,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                 reservation_coordinator: IThreadReservationCoordinator,
                 actions_resolver: DynamicResourceActionResolver,
                 executing_method_registry: ExecutingMethodRegistry,
                 system_map: SystemMap,
                 labware_location_service: ILabwareLocationService,
                 coordination_config: CoordinationConfig | None = None,
                 method_factory: MethodFactory | None = None,
                 method_registry: IMethodRegistry | None = None,
                 variable_store: IVariableResolver | None = None,
                 register_method_template: Callable[[str, MethodTemplate], None] | None = None,
                 residency_check: ResidencyCheck | None = None,
                 ) -> None:
        self._event_bus = event_bus
        self._actions_resolver = actions_resolver
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._reservation_coordinator = reservation_coordinator
        self._system_map = system_map
        self._executing_method_registry = executing_method_registry
        self._labware_location_service = labware_location_service
        self._coordination_config = coordination_config
        self._method_factory = method_factory
        self._method_registry = method_registry
        self._variable_store = variable_store
        self._register_method_template = register_method_template
        self._residency_check = residency_check
        # S3 Round 1.5: late-bound. The factory is constructed during
        # SystemBuild (SdkToSystemBuilder), BEFORE SystemRuntime exists.
        # SystemRuntime.__init__ calls set_thread_incident_declarer(self) so every
        # thread minted after construction (i.e., during execution) gets
        # the back-ref. Threads minted before set_thread_incident_declarer fires
        # would get None -- there are none in practice (threads spawn only
        # during execution), but the optional-None type makes test fixtures
        # that bypass SystemRuntime trivial.
        self._thread_incident_declarer: IThreadIncidentDeclarer | None = None
        self._labware_placer: LabwarePlacer | None = None

    def set_labware_placer(self, placer: LabwarePlacer) -> None:
        """Set the placement chokepoint back-reference (late-bound by System).

        Every thread minted after this returns projects its spawn-placed labware
        onto the LH deck (no-op off a liquid handler) through it."""
        self._labware_placer = placer

    def set_thread_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Set the post-construction back-reference for thread-incident recording.

        Called by SystemRuntime.__init__ once it has constructed itself
        and can serve as the IThreadIncidentDeclarer implementation. Every
        thread created after this returns will receive the back-ref.
        """
        self._thread_incident_declarer = declarer

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
            labware_placer=self._labware_placer,
            residency_check=self._residency_check,
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
    def set_labware_placer(self, placer: LabwarePlacer) -> None:
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

    def set_labware_placer(self, placer: LabwarePlacer) -> None:
        """Forward the placement chokepoint down to the factory (late-bound by
        System after construction, like the incident declarer)."""
        self._factory.set_labware_placer(placer)

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