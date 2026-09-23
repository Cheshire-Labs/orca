import asyncio
import logging
import uuid
from typing import AsyncGenerator, List, Set

from orca.async_util import drain_cancelled_waiters
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.events.event_bus_interface import IEventBus
from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import ExecutionContext, MethodExecutionContext, WorkflowExecutionContext
from orca.state.records import (
    DeclaredTracking,
    DeclaredVolumeTransfer,
    DeviceOperation,
    OperationDetails,
)
from orca.resource_models.tracking_context import TrackingContext
from orca.runtime.group_execution_context import GroupExecutionContext
from orca.variables.variable_store import IVariableResolver, NullVariableResolver
from orca.workflow_models.actions.assigned_location_action import AssignedLocationAction
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver, UnresolvedLocationAction
from orca.workflow_models.actions.util import IActionReservationStatusSink
from orca.workflow_models.interfaces import IHasLabware, IMethod
from orca.workflow_models.merge_lane import DroppedAnchorInsert, MergeLane
from orca.workflow_models.method_state_machine import MethodEvent, MethodStateMachine
from orca.workflow_models.shared_action_coordination import (
    ActionResolution,
    SharedActionCoordination,
)
from orca.workflow_models.shared_method_coordination import SharedMethodCoordination
from orca.workflow_models.status_enums import ActionStatus, FailurePolicy, MethodStatus, RecoveryDecision
from orca.workflow_models.status_manager import StatusManager

orca_logger = logging.getLogger("orca")


class MethodResolutionAbortedError(Exception):
    """Raised by ``wait_for_current_action`` when the shared method
    completes or aborts before the owner thread binds ``_current_action``.

    A contributor parked on the resolution event would otherwise block
    forever in that case. The caller's exception handler routes this
    through ``_pause_for_error`` so the operator (or the auto-mode
    classifier) decides RETRY vs. ABORT_THREAD.
    """


class SharedRendezvousResolved(Exception):
    """Raised by ``wait_for_current_action`` when the owner published the slot
    outcome before binding an action (a pre-binding failure). Carries that
    outcome so the contributor reacts to the owner's decision without pausing."""

    def __init__(self, outcome: ActionResolution) -> None:
        super().__init__(f"shared rendezvous resolved pre-binding: {outcome.decision}")
        self.outcome = outcome


async def _list_to_generator(items: List[UnresolvedLocationAction]) -> AsyncGenerator[UnresolvedLocationAction, None]:
    for item in items:
        yield item


async def _await_future_done(future: asyncio.Future[ActionResolution]) -> bool:
    """Wait until ``future`` resolves without consuming or cancelling it. Shielded
    so cancelling this waiter leaves the shared slot future intact for others."""
    await asyncio.shield(future)
    return True


class MethodInstance(IMethod):
    def __init__(
        self,
        name: str,
        failure_policy: FailurePolicy | None = None,
    ) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._actions: List[UnresolvedLocationAction] = []
        self._method_failure_policy = failure_policy or FailurePolicy.PAUSE

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def method_failure_policy(self) -> FailurePolicy:
        return self._method_failure_policy

    @property
    def actions(self) -> List[UnresolvedLocationAction]:
        return self._actions

    def append_action(self, action: UnresolvedLocationAction) -> None:
        self._actions.append(action)

    def assign_thread(
        self,
        input_template: LabwareTemplate,
        thread: IHasLabware,
    ) -> None:
        for step in self.actions:
            step.try_assign_labware(input_template, thread.labware)


class ExecutingMethod(IMethod):
    """Runtime wrapper for a method. Always wraps a MergeLane of UnresolvedLocationActions.

    All method forms (declarative, generator, legacy code method) converge here
    as a generator of UnresolvedLocationActions fed through a MergeLane.
    Generator methods and code-first actions are driven by the yield adapter
    at thread level, not here.
    """

    def __init__(self, method: IMethod, event_bus: IEventBus, status_manager: StatusManager, context: WorkflowExecutionContext, variable_store: IVariableResolver | None = None, tracking_context: TrackingContext | None = None) -> None:
        self._event_bus = event_bus
        self._status_manager = status_manager
        self._context = context
        self._method = method
        self._variable_store: IVariableResolver = variable_store or NullVariableResolver()
        self._tracking_context = tracking_context

        # Single path: wrap action list in MergeLane.
        # Generator methods produce actions via the yield adapter at thread level,
        # which creates individual ExecutingMethods each wrapping a single action.
        # name_getter uses tag only: Before/After anchors target tagged actions only.
        self._action_lane: MergeLane[UnresolvedLocationAction] = MergeLane(
            _list_to_generator(list(method.actions)),
            name_getter=lambda a: a.tag,
        )

        self._current_action: ExecutableLocationAction | None = None
        self._current_unresolved_action: UnresolvedLocationAction | None = None
        self._current_assigned_action: AssignedLocationAction | None = None
        self._completed_actions: List[ExecutableLocationAction] = []
        self._index = 0
        self._current_thread_id: str | None = None
        self._current_thread_name: str | None = None
        self._participating_thread_ids: set[str] = set()
        self._current_submission_id: str | None = None
        self._participating_thread_names: dict[str, str] = {}
        self._was_skipped: bool = False
        self._was_aborted: bool = False
        self._dropped_anchor_inserts: list[DroppedAnchorInsert] = []
        self._recovery_generation: int = 0
        self._wait_event_name: str | None = None
        self._wait_timeout: float | None = None
        self._state_machine = MethodStateMachine()
        self._shared_coord = SharedMethodCoordination()
        # Action-scoped participant group for the current shared action, minted per
        # slot. Sibling of _shared_coord (method-scoped); the levels never cross.
        self._current_action_coord: SharedActionCoordination | None = None
        self._action_completion_tasks: Set[asyncio.Task[None]] = set()
        self._publish_status_to_status_manager(self._state_machine.current)
        self._event_channel_registry: EventChannelRegistry | None = None
        self._partner_constraints: dict[str, dict[str, str]] = {}
        # Contributor thread's group/submission identity, used by the
        # auto-spawn callback to compose group-aware slot keys. Populated
        # just before the callback fires; survives drain-and-redeliver.
        self._contributor_context: GroupExecutionContext | None = None
        # Per-receiver 0-based contribution index, stamped by the auto-spawn callback.
        self._pool_indices: dict[str, int] = {}

    @property
    def contributor_context(self) -> GroupExecutionContext | None:
        return self._contributor_context

    def set_contributor_context(self, context: GroupExecutionContext) -> None:
        self._contributor_context = context

    @property
    def pool_indices(self) -> dict[str, int]:
        return self._pool_indices

    def set_pool_index(self, receiver_name: str, index: int) -> None:
        self._pool_indices[receiver_name] = index

    def set_partner_constraints(self, template_name: str, constraints: dict[str, str]) -> None:
        self._partner_constraints[template_name] = constraints

    def get_partner_constraints(self, template_name: str) -> dict[str, str] | None:
        return self._partner_constraints.get(template_name)

    def set_event_channel_registry(self, registry: EventChannelRegistry) -> None:
        self._event_channel_registry = registry

    @property
    def id(self) -> str:
        return self._method.id

    @property
    def name(self) -> str:
        return self._method.name

    @property
    def actions(self) -> List[UnresolvedLocationAction]:
        return self._method.actions

    def aggregate_demand(self, labware_name: str) -> DeclaredTracking | None:
        """Union of every action's ``declares`` referencing ``labware_name``.

        Consumed by the auto-spawn gate at executing_workflow.py so the
        receiver's can_continue(demand) sees the whole method's needs, not
        just one action's. Filter keys on labware name: wells_used /
        tips_used entries that key to ``labware_name`` contribute; transfers
        contribute when source or target matches. ``operations`` pass
        through unfiltered (authors opt into them for per-labware intent;
        consumer matches by details.labware).
        """
        all_wells: dict[str, list[str]] = {}
        all_tips: dict[str, list[str]] = {}
        all_transfers: list[DeclaredVolumeTransfer] = []
        all_ops: list[tuple[DeviceOperation, OperationDetails]] = []
        seen = False
        for action in self._method.actions:
            d = action.declares
            if d is None:
                continue
            seen = True
            if d.wells_used:
                values = d.wells_used.get(labware_name)
                if values:
                    all_wells.setdefault(labware_name, []).extend(values)
            if d.tips_used:
                values = d.tips_used.get(labware_name)
                if values:
                    all_tips.setdefault(labware_name, []).extend(values)
            if d.volume_transferred:
                for vt in d.volume_transferred:
                    if vt.source == labware_name or vt.target == labware_name:
                        all_transfers.append(vt)
            if d.operations:
                all_ops.extend(d.operations)
        if not seen:
            return None
        return DeclaredTracking(
            wells_used=all_wells or None,
            volume_transferred=all_transfers or None,
            tips_used=all_tips or None,
            operations=all_ops or None,
        )

    def append_action(self, action: UnresolvedLocationAction) -> None:
        self._method.append_action(action)

    def assign_thread(
        self,
        input_template: LabwareTemplate,
        thread: IHasLabware,
    ) -> None:
        self._method.assign_thread(input_template, thread)

    @property
    def wait_event_name(self) -> str | None:
        """Non-None when this ExecutingMethod is a WaitStep sentinel."""
        return self._wait_event_name

    @property
    def wait_timeout(self) -> float | None:
        return self._wait_timeout

    def set_wait_step(self, event_name: str, timeout: float | None) -> None:
        """Mark this ExecutingMethod as a WaitStep sentinel."""
        self._wait_event_name = event_name
        self._wait_timeout = timeout

    @property
    def recovery_generation(self) -> int:
        return self._recovery_generation

    @property
    def is_wait_step(self) -> bool:
        return self._wait_event_name is not None

    @property
    def action_lane(self) -> MergeLane[UnresolvedLocationAction]:
        return self._action_lane

    @property
    def dropped_anchor_inserts(self) -> list[DroppedAnchorInsert]:
        """Anchored action inserts still pending when the method ended.

        Read live from the lane on normal completion (the lane is never
        closed there). ``abort()`` closes the lane, so it stashes the
        dropped inserts first and this returns the stash.
        """
        if self._dropped_anchor_inserts:
            return self._dropped_anchor_inserts
        return self._action_lane.unresolved_anchor_inserts()

    @property
    def completed_actions(self) -> List[ExecutableLocationAction]:
        return self._completed_actions

    @property
    def current_action(self) -> ExecutableLocationAction | None:
        return self._current_action

    def has_completed(self) -> bool:
        return self._action_lane.exhausted and MethodStatus.COMPLETED == self.status

    @property
    def status(self) -> MethodStatus:
        return self._state_machine.current

    @property
    def shared_coord(self) -> SharedMethodCoordination:
        return self._shared_coord

    @property
    def current_action_coord(self) -> SharedActionCoordination | None:
        """The action-scoped participant group for the current shared action, or None
        before the first slot is minted. Used by owner and contributors to pause,
        submit the recovery decision, and fan out to the outcome as a unit."""
        return self._current_action_coord

    def _fire(self, event: MethodEvent) -> None:
        new_status = self._state_machine.transition(event)
        self._publish_status_to_status_manager(new_status)

    def _publish_status_to_status_manager(self, status: MethodStatus) -> None:
        context = MethodExecutionContext(execution_id=self._context.execution_id,
                                        workflow_name=self._context.workflow_name,
                                        method_id=self._method.id,
                                        method_name=self._method.name,
                                        thread_id=self._current_thread_id,
                                        thread_name=self._current_thread_name,
                                        participating_thread_ids=tuple(self._participating_thread_ids))
        self._status_manager.set_status("METHOD",
                                        self._method.id,
                                        status.name,
                                        context)

    def set_current_thread(
        self, thread_id: str, thread_name: str, submission_id: str | None = None,
    ) -> None:
        """Set the thread currently executing this method. For shared methods,
        each participating thread calls this, building the full participant list.

        The submission rides along because an action body resolves variables
        submission-first, and this is the only point where the thread running
        the method is known.
        """
        self._current_thread_id = thread_id
        self._current_thread_name = thread_name
        self._current_submission_id = submission_id
        self._participating_thread_ids.add(thread_id)
        self._participating_thread_names[thread_id] = thread_name

    @property
    def participating_thread_ids(self) -> set[str]:
        return self._participating_thread_ids

    @property
    def was_skipped(self) -> bool:
        return self._was_skipped

    @property
    def was_aborted(self) -> bool:
        return self._was_aborted

    @property
    def exit_signal(self) -> asyncio.Event:
        """Internal: set by either mark_skipped() or abort().
        Used to interrupt co-labware waits."""
        return self._shared_coord.exit_signal

    @property
    def completed(self) -> asyncio.Event:
        """Terminal-lifecycle signal. Set when the method reaches a
        terminal status (COMPLETED / SKIPPED / PARTIAL_COMPLETE)."""
        return self._shared_coord.completed

    def mark_skipped(self) -> None:
        """Skip this method. Only valid before execution starts (CREATED)."""
        if self.status != MethodStatus.CREATED:
            raise ValueError(
                f"Cannot skip method '{self.name}' (status={self.status.name}). "
                f"Use abort() for in-progress methods."
            )
        self._was_skipped = True
        self._shared_coord.exit_signal.set()
        self._fire(MethodEvent.MARK_SKIPPED)
        self._shared_coord.completed.set()

    @property
    def workflow_context(self) -> WorkflowExecutionContext:
        return self._context

    def _handle_action_completed(self, event: str, context: ExecutionContext) -> None:
        task = asyncio.create_task(self._handle_action_completed_async(event, context))
        self._action_completion_tasks.add(task)
        task.add_done_callback(self._on_action_completion_task_done)

    def _on_action_completion_task_done(self, task: asyncio.Task[None]) -> None:
        self._action_completion_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            orca_logger.error(
                "Action-completion handler for method '%s' raised: %s",
                self._method.name,
                exc,
                exc_info=exc,
            )


    async def _handle_action_completed_async(self, event: str, context: ExecutionContext) -> None:
        async with self._shared_coord.resolving_action_lock:
            assert self._current_action is not None, "Current action should not be None when handling action completion."
            action_coord = self._current_action_coord
            self._event_bus.unsubscribe(event, self._handle_action_completed)
            completed_action = self._current_action
            self._completed_actions.append(completed_action)
            self._current_action = None
            self._current_unresolved_action = None
            self._current_assigned_action = None
            # Reset resolution event so the next action's contributor waiters
            # block correctly. ``current_action_resolved`` is set again when
            # the next action is bound in resolve_current_action.
            self._shared_coord.current_action_resolved.clear()
            # Publish COMPLETED only after the slot state is wound down (still under
            # the lock, captured group) so a contributor detaches after the clear, not before.
            if action_coord is not None:
                action_coord.publish_outcome(ActionResolution(None))

            # Eagerly check if the lane has more actions via peek.
            # This prevents a race where the thread loop re-enters
            # consume_next_unresolved_action() before the handler clears _current_action.
            peeked = await self._action_lane.peek()
            if peeked is None and not self._state_machine.is_terminal():
                if self._was_aborted:
                    self._fire(MethodEvent.METHOD_ABORTED)
                else:
                    self._fire(MethodEvent.ALL_ACTIONS_COMPLETED)
                self._shared_coord.completed.set()

    async def consume_next_unresolved_action(self, action_resolver: DynamicResourceActionResolver) -> UnresolvedLocationAction | None:
        """Consume the next action from the lane WITHOUT reserving a device.

        Returns the UnresolvedLocationAction so the caller can inspect its
        resource pool before committing to a reservation. Returns None if
        the method is exhausted.
        """
        async with self._shared_coord.resolving_action_lock:
            if self._current_action is not None:
                return self._current_unresolved_action
            if self._current_unresolved_action is not None:
                return self._current_unresolved_action

            # Mint the action group BEFORE binding so a pre-binding failure can still
            # resolve it; RETRY re-drives the live action and never re-mints.
            self._current_action_coord = SharedActionCoordination()

            # Consume from lane, skipping flagged actions
            while True:
                try:
                    current_dynamic_action = await self._action_lane.next()
                except StopAsyncIteration:
                    if not self._state_machine.is_terminal():
                        if self._was_aborted:
                            self._fire(MethodEvent.METHOD_ABORTED)
                        else:
                            self._fire(MethodEvent.ALL_ACTIONS_COMPLETED)
                        self._shared_coord.completed.set()
                    return None

                if self._action_lane.should_skip(current_dynamic_action.command):
                    continue
                if current_dynamic_action.was_skipped:
                    continue
                break

            if self.status == MethodStatus.CREATED:
                self._fire(MethodEvent.ACTION_CONSUMED)
            self._current_unresolved_action = current_dynamic_action
            return current_dynamic_action

    async def resolve_current_action(
        self,
        thread_id: str,
        current_location: Location,
        action_resolver: DynamicResourceActionResolver,
        requesting_labware: LabwareInstance | None = None,
        status_sink: IActionReservationStatusSink | None = None,
    ) -> ExecutableLocationAction:
        """Reserve a device for the already-consumed unresolved action.

        Must be called after consume_next_unresolved_action() returned non-None.
        For shared methods, only the OWNER thread (one that yields the method
        directly, not via orca.join) calls this. Contributors await
        ``wait_for_current_action`` instead; see that method's docstring.

        ``requesting_labware`` (Round 5 S1-B) lets the reservation layer
        recognize own-labware-at-target as a non-conflict. Without it,
        an entry thread whose own labware sits at its own start_location
        rejected forever because the reservation layer could not tell own
        labware from cross-thread occupancy.

        ``status_sink`` lets the owner thread surface ``AWAITING_ACTION_RESERVATION``
        with the candidate location list while the reservation-retry loop
        cycles -- without it the thread sits silently in
        ``RESOLVING_ACTION_LOCATION`` with no ``waiting_for`` field
        populated. Contributors don't need it because they park on
        ``wait_for_current_action``, not the reservation loop.
        """
        async with self._shared_coord.resolving_action_lock:
            if self._current_action is not None:
                return self._current_action

            assert self._current_unresolved_action is not None, (
                "Must call consume_next_unresolved_action() first"
            )

            assigned = await action_resolver.resolve_action(
                thread_id, self._current_unresolved_action, current_location,
                requesting_labware=requesting_labware,
                status_sink=status_sink,
            )
            self._current_assigned_action = assigned
            self._current_action = self._create_executable_action(assigned)
            self._subscribe_to_current_action()
            # Wake any contributor threads parked in wait_for_current_action.
            self._shared_coord.current_action_resolved.set()
            return self._current_action

    async def wait_for_current_action(self) -> ExecutableLocationAction:
        """Contributor-side wait for the owner thread to bind _current_action.

        Shared methods route their device reservation through the OWNER
        (the thread that yields the method directly, not via orca.join).
        Contributor threads (joined via orca.join; registered through
        ``shared_coord.add_contributor(thread_id)``) must NOT call
        resolve_current_action themselves -- the lock is held across the
        await on the reservation retry loop, and a contributor's
        reservation request carries the contributor's thread_id which
        won't match the holder's hold-over and will be rejected
        indefinitely, blocking the owner from ever acquiring the lock.
        Instead, contributors await ``current_action_resolved``, then read
        the cached ``_current_action`` that the owner produced.

        Races binding against the slot outcome so a pre-binding owner failure
        reaches a parked contributor: if the owner publishes the slot outcome
        before an action binds, this raises ``SharedRendezvousResolved`` carrying
        that outcome so the caller reacts without pausing. ``completed`` stays in
        the race as a defensive fallback (``abort()`` publishes on the slot
        first, so it is not reached in the real flow) raising
        ``MethodResolutionAbortedError``.
        """
        if self._current_action is not None:
            return self._current_action

        action_coord = self._current_action_coord
        resolved_task = asyncio.create_task(self._shared_coord.current_action_resolved.wait())
        completed_task = asyncio.create_task(self.completed.wait())
        race: List[asyncio.Task[bool]] = [resolved_task, completed_task]
        if action_coord is not None:
            race.append(asyncio.create_task(_await_future_done(action_coord.outcome)))
        try:
            await asyncio.wait(race, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # The slot waiter is shielded, so cancelling it leaves the
            # shared slot future intact for others.
            await drain_cancelled_waiters(*race)

        if self._current_action is not None:
            return self._current_action

        # Pre-binding: the owner published a terminal outcome before binding an
        # action. Surface it so the contributor reacts without pausing.
        if action_coord is not None and action_coord.outcome.done():
            raise SharedRendezvousResolved(action_coord.outcome.result())

        # _current_action is None. Two shapes raise the typed error:
        # (a) ``abort()`` ran before any owner bound a current action. The
        #     ``_was_aborted`` flag distinguishes this from normal completion.
        # (b) ``_handle_action_completed_async`` set ``completed`` after the
        #     lane exhausted, without ``_was_aborted``. Not expected for a
        #     contributor in current code (action execution gates on
        #     ``all_labware_is_present``, which requires this contributor's
        #     labware, so the contributor must have already returned from
        #     ``wait_for_current_action`` before the action could complete);
        #     surfaced as a typed error for diagnosability if the invariant
        #     is ever broken. The log line below leaves a trail.
        if self._was_aborted:
            raise MethodResolutionAbortedError(
                f"shared method '{self._method.name}' aborted before "
                f"contributor's action could resolve"
            )
        orca_logger.warning(
            "wait_for_current_action observed completed=set without "
            "_was_aborted on method '%s'; expected invariant "
            "(all_labware_is_present gates action execution) is broken or "
            "contributor woke past the resolved-event without seeing the "
            "bound action.",
            self._method.name,
        )
        raise MethodResolutionAbortedError(
            f"shared method '{self._method.name}' completed without binding "
            f"a current action for contributor"
        )

    async def resolve_next_action(self, thread_id: str, current_location: Location, action_resolver: DynamicResourceActionResolver) -> ExecutableLocationAction | None:
        """Consume and reserve the next action in one call (backward compat)."""
        unresolved = await self.consume_next_unresolved_action(action_resolver)
        if unresolved is None:
            return None
        return await self.resolve_current_action(thread_id, current_location, action_resolver)

    @property
    def current_failure_policy(self) -> FailurePolicy:
        """Failure policy of the action currently being executed.

        Raises ValueError if no action is executing, since the failure
        policy is only meaningful in the context of an active action.
        A silent fallback to PAUSE would mask broken call sites.
        """
        if self._current_unresolved_action is None:
            raise ValueError(
                "No action is currently executing; cannot determine failure policy"
            )
        return self._current_unresolved_action.failure_policy

    def _action_completed_event_name(self) -> str:
        assert self._current_action is not None
        return f"ACTION.{self._current_action.action.id}.{ActionStatus.COMPLETED.name}"

    def _subscribe_to_current_action(self) -> None:
        self._event_bus.subscribe(self._action_completed_event_name(), self._handle_action_completed)

    def _unsubscribe_current_action(self) -> None:
        if self._current_action is not None:
            self._event_bus.unsubscribe(self._action_completed_event_name(), self._handle_action_completed)

    def retry_current_action(self) -> None:
        """Retry the failed action without re-resolution, keeping
        the reservation held continuously so no other thread can
        claim the location during the retry window.

        Bypasses ResourcePoolResolver entirely since the plate is
        already at the target location.
        """
        assert self._current_assigned_action is not None
        self._unsubscribe_current_action()
        self._current_action = self._create_executable_action(self._current_assigned_action)
        self._subscribe_to_current_action()

    async def advance_past_current_action(self) -> None:
        """Drop the failed action off the lane and move to the next one.

        Shared by the two decisions that carry the method on past a failure:
        ABORT_ACTION (the action is discarded) and CONTINUE (the operator has
        dealt with it). Neither re-runs it.

        Clears ``current_action_resolved`` so contributors awaiting the
        next action re-park instead of waking on the prior action's
        latched signal (which would observe ``_current_action is None``
        and raise ``MethodResolutionAbortedError``). Mirrors the
        state-reset shape in ``_handle_action_completed_async``.
        """
        async with self._shared_coord.resolving_action_lock:
            self._unsubscribe_current_action()
            self._current_action = None
            self._current_unresolved_action = None
            self._current_assigned_action = None
            self._shared_coord.current_action_resolved.clear()
            peeked = await self._action_lane.peek()
            if peeked is None and not self._state_machine.is_terminal():
                if self._was_aborted:
                    self._fire(MethodEvent.METHOD_ABORTED)
                else:
                    self._fire(MethodEvent.ALL_ACTIONS_COMPLETED)
                self._shared_coord.completed.set()

    async def abort(self) -> None:
        """Abort this method mid-execution. Abandons remaining actions,
        signals waiting threads via exit_signal. Cancels running user tasks
        for code method actions."""
        if self.status == MethodStatus.COMPLETED:
            return
        if self.status == MethodStatus.CREATED:
            raise ValueError(
                f"Cannot abort method '{self.name}' (not started). "
                f"Use mark_skipped() for pending methods."
            )
        self._was_aborted = True
        # Fan out to the action group: an external abort (mutation coordinator)
        # resolves the action even without an operator decision.
        action_coord = self._current_action_coord
        if action_coord is not None:
            action_coord.publish_outcome(
                ActionResolution(RecoveryDecision.ABORT_METHOD)
            )
        self._shared_coord.exit_signal.set()
        self._unsubscribe_current_action()
        if self._current_action is not None:
            self._current_action.action.cancel_user_task()
        self._current_action = None
        self._current_unresolved_action = None
        self._current_assigned_action = None
        self._dropped_anchor_inserts = await self._action_lane.close()
        async with self._shared_coord.resolving_action_lock:
            if not self._state_machine.is_terminal():
                self._fire(MethodEvent.METHOD_ABORTED)
        self._shared_coord.completed.set()

    async def handle_recovery(self, decision: RecoveryDecision, generation: int | None = None) -> None:
        """Apply a recovery decision to the current failed action.

        On a shared action only the OWNER thread calls this; contributors feed
        the group's single decision via `group.submit_decision` and never recover
        the method themselves. `generation` is a stale-apply guard: the caller
        snapshots `recovery_generation` before pausing and passes it back, so a
        decision computed against an already-superseded failure is a no-op.
        Single-thread methods pass None and skip the check.
        """
        if generation is not None and generation != self._recovery_generation:
            return

        if self._current_action is None:
            if generation is None:
                raise AssertionError("Cannot handle recovery without a current action")
            return

        self._recovery_generation += 1
        # Capture the action group before applying the decision, then publish the
        # terminal outcome to participants; RETRY/RETRY_OP re-drive and publish nothing.
        action_coord = self._current_action_coord

        if decision == RecoveryDecision.RETRY:
            self.retry_current_action()
            return
        if decision == RecoveryDecision.CONTINUE:
            # Ledger first: dropping the action drops its operation log with it.
            await self._current_action.record_operator_continued()
            self._current_action.action.release_reservation()
            await self.advance_past_current_action()
        elif decision == RecoveryDecision.ABORT_ACTION:
            # An abort drops the action's operation log with it, so what the
            # action really did is lost. Say so before letting go of it.
            await self._current_action.record_operations_dropped()
            self._current_action.action.release_reservation()
            await self.advance_past_current_action()
        elif decision == RecoveryDecision.ABORT_METHOD:
            await self._current_action.record_operations_dropped()
            self._current_action.action.release_reservation()
            await self.abort()
        elif decision == RecoveryDecision.ABORT_THREAD:
            await self._current_action.record_operations_dropped()
            self._current_action.action.release_reservation()
            self._unsubscribe_current_action()
            self._current_action = None
            self._current_unresolved_action = None
            self._current_assigned_action = None
        else:
            return

        if action_coord is not None:
            action_coord.publish_outcome(ActionResolution(decision))

    def _create_executable_action(self, assigned: AssignedLocationAction) -> ExecutableLocationAction:
        # The previous shape omitted ``thread_id`` /
        # ``thread_name`` / ``participating_thread_ids``, which left
        # ``ExecutableLocationAction._process_tracking`` with no source
        # for thread attribution. Every ops_history record landed with
        # ``thread_id=""`` and the search-by-thread filter was a no-op.
        # Mirror the ``status.setter`` shape on this class so the rich
        # context flows through to tracking observers.
        context = MethodExecutionContext(execution_id=self._context.execution_id,
                                        workflow_name=self._context.workflow_name,
                                        method_id=self._method.id,
                                        method_name=self._method.name,
                                        thread_id=self._current_thread_id,
                                        thread_name=self._current_thread_name,
                                        participating_thread_ids=tuple(self._participating_thread_ids))
        return assigned.executable(
            self._status_manager,
            context,
            self._variable_store,
            self._event_channel_registry,
            self._tracking_context,
            self._pool_indices,
            self._current_submission_id,
        )
