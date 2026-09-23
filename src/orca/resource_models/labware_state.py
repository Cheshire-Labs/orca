"""Labware lifecycle state and registry for tracking labware across journeys."""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Protocol, runtime_checkable

from orca.async_util import drain_cancelled_waiters
from orca.events.event_bus_interface import IEventBus
from orca.events.execution_context import ExecutionContext
from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.thread_template import ThreadTemplate


class IWorkflowRef(Protocol):
    """Minimal workflow surface needed by overflow strategies.

    Defined here so labware_state.LabwareSlot can type its overflow_strategy
    field without a circular import back through workflow_models.
    ExecutingWorkflow satisfies this structurally.
    """

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def event_bus(self) -> IEventBus: ...


@runtime_checkable
class IOverflowStrategy(Protocol):
    """Called by the spawn callback when a slot rejects a new enqueue.

    Receives the slot, the method that was rejected, and a workflow reference.
    Users implementing custom strategies import LabwareSlot, ExecutingMethod
    from orca.*; the workflow ref is structurally typed (id/name/event_bus).
    """

    def on_overflow(self, slot: "LabwareSlot", method: ExecutingMethod,
                    workflow: IWorkflowRef) -> None: ...


class LabwareState(str, Enum):
    """Lifecycle state of a labware instance within a workflow execution."""
    AVAILABLE = "AVAILABLE"
    IN_JOURNEY = "IN_JOURNEY"
    PARKED = "PARKED"
    ENDED = "ENDED"


class IRegisteredThread(Protocol):
    def has_completed(self) -> bool: ...
    def has_finished_its_work(self) -> bool: ...
    def stop(self) -> None: ...
    @property
    def labware_template(self) -> LabwareTemplate | None: ...
    @property
    def thread_instance(self) -> LabwareThreadInstance: ...


@dataclass
class RegisteredLabware:
    """A labware instance tracked by the registry with its current state."""
    instance: LabwareInstance
    state: LabwareState
    template_name: str
    thread: IRegisteredThread | None = None


@dataclass
class Found:
    """A parked thread was found and claimed (transitioned to IN_JOURNEY)."""
    registered: RegisteredLabware


class NoneAvailable:
    """No instances of this template type exist in PARKED state."""
    pass


@dataclass
class NoMatch:
    """Parked instances exist but none match the given constraints."""
    reason: str


FindResult = Found | NoneAvailable | NoMatch


class _SlotClosedSentinel:
    """Singleton type for SLOT_CLOSED_SENTINEL; semantics on the constant below."""
    __slots__ = ()


SLOT_CLOSED_SENTINEL: _SlotClosedSentinel = _SlotClosedSentinel()
"""Placed in a LabwareSlot.queue by close() to wake any receiver blocked
on queue.get(). A wakeup, not a verdict: consumers discard it and re-test
``is_closed and queue.empty()``, so work queued behind it is still served."""


SlotQueueItem = ExecutingMethod | _SlotClosedSentinel


@dataclass
class LabwareSlot:
    """Per-template work slot: queue + active thread reference + capacity state.

    The queue is the single mechanism for delivering shared methods to threads.
    Spawn callback enqueues (sync via put_nowait). JoinTemplate consumes
    (async via queue.get). ParkTemplate never touches the queue.

    Capacity fields (policy, contributions_to_active, pending) are the
    slot-level CapacityPolicy plumbing. policy is a cache of the workflow
    template's declared policy, populated on fresh-receiver creation.
    pending holds unbound stashed items from two sources: overflow items
    stashed by SequentialStashStrategy (drained at handoff when the active
    receiver ends) and contributions arriving while the slot is orphaned
    (disposed of by the accept-partial resume drain).

    overflow_strategy holds a user-supplied IOverflowStrategy if the
    workflow attached one via `wf.thread(..., overflow_strategy=...)`.
    The Protocol lives in this module (rather than workflow_models.overflow_strategy)
    to avoid a circular import: overflow_strategy.py depends on LabwareSlot,
    so the reverse dependency for type-checking lives here instead.

    close/is_closed: the contribution-window flag + wakeup sentinel for the
    adaptive submission opportunity window. ExecutingWorkflow._evaluate_slot_closures
    fires close() once the slot's contribution window is over (its declared
    contributes_to feeders have terminated, or an unfed receiver has quiesced
    or been drained); the sentinel wakes a receiver blocked on queue.get().
    Work queued behind the sentinel is still served: ctx.has_more_work()
    returns True while the queue is non-empty even on a closed slot, and
    turns False only once the slot is closed AND drained (see close()).

    slot_key is the composite lookup key (e.g. "tips:*:<submission_id>")
    used by the registry's _slots dict. labware_template_name is the
    bare labware template name (e.g. "tips") passed to auto-spawn
    callbacks so get_auto_spawn_template lookups resolve correctly.
    """
    slot_key: str
    labware_template_name: str
    queue: asyncio.Queue[SlotQueueItem] = field(default_factory=asyncio.Queue)
    active_thread: IRegisteredThread | None = None
    contributions_to_active: int = 0
    pending: deque[ExecutingMethod] = field(default_factory=deque)
    policy: CapacityPolicy | None = None
    overflow_strategy: IOverflowStrategy | None = None
    is_closed: bool = False
    receiver_drained: bool = False
    """Set True when the active receiver's user generator has returned.
    Distinguishes "still able to consume orca.join() contributions" from
    "moving home but not yet COMPLETED." Reset to False whenever a fresh
    receiver claims the slot."""
    receiver_spent: bool = False
    """Set True when the active receiver's labware answered can_continue()
    False: it has nothing left to give, whatever room the slot still has.

    The bind gate is the only place that can ask (can_continue is async and
    the receiver's exit test is deliberately sync), so it records the answer
    here. It SURVIVES the handoff and is reset only when a fresh receiver
    claims the slot, because the replacement has to know it exists to take
    over from used-up labware rather than adopt it."""
    spawn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serializes this slot's check-then-create in the spawn callback: two
    contributors firing concurrently would otherwise both see active_thread=None
    across the create await and each mint a duplicate receiver. Slot-scoped
    analogue of Location.spawn_lock (reuse-bind); the two nest, not redundant."""
    orphaned: bool = False
    """Quarantine flag: the active receiver reached an abnormal terminal
    (ABORTED/STOPPED) with undelivered items still owed. Closure evaluation
    skips the slot, the spawn callback stashes instead of minting (backlog
    methods are bound to the dead thread, so re-routing is never legal), and
    only the operator's execution-level resume disposes of the backlog."""
    orphaned_in_flight: ExecutingMethod | None = None
    """The dead receiver's dequeued-but-incomplete contribution, snapshotted
    at quarantine time. Carried here because the contributor registration is
    removed when the dead thread's lane closes, long before resume, and
    ``active_thread`` is typed too narrowly to re-read it at drain time."""
    awaiting_threads: list[IRegisteredThread] = field(default_factory=list)
    """Threads currently suspended empty-handed in await_next_method, recorded
    by EXPLICIT CALLER IDENTITY (the waiter reports itself; never inferred from
    active_thread, which a colliding slot key or an injected recovery receiver
    can make wrong). Purely reactive threads: they resolve no actions, so they
    demand nothing; closure evaluation excludes waiters of OPEN slots from
    every slot's worker set. Appended on entry, removed on every exit; multiple
    concurrent waiters coexist. Distinct from LabwareState.PARKED /
    orca.park(): those describe labware at rest between assignments - at one
    of its author-declared park spots, or wherever it stood when arriving
    work preempted the park (the engine never relocates parked labware)."""
    on_awaiting_join: Callable[[], None] | None = None
    """Workflow-wired hook, fired synchronously on an empty-handed
    await_next_method entry AFTER the waiter is recorded, so closure
    evaluation observes the wait that may complete an all-waiting state."""

    def has_active_thread(self) -> bool:
        if self.active_thread is None:
            return False
        if self.active_thread.has_completed():
            return False
        if self.receiver_drained:
            return False
        return True

    def has_room(self) -> bool:
        if self.policy is None:
            return True
        return self.contributions_to_active < self.policy.max_contributions

    def mark_receiver_spent(self) -> None:
        """Record that the active receiver's labware is used up. Idempotent.

        The flag makes the receiver's next ``ctx.has_more_work()`` answer
        False; the sentinel (a wakeup, not a verdict, exactly as in close())
        lifts a receiver ALREADY parked in ``await_next_method``, which never
        re-reads has_more_work. Depletion is found by a contributor, at a
        moment the receiver has no say in, so it lands either side of the wait.
        """
        if self.receiver_spent:
            return
        self.receiver_spent = True
        self.queue.put_nowait(SLOT_CLOSED_SENTINEL)

    def close(self) -> None:
        """Mark the contribution window over. Idempotent.

        ``is_closed`` is the state; the sentinel is ONLY a wakeup, enqueued to
        unblock a receiver already parked in ``queue.get()`` so it can re-test
        its exit condition. It is not a terminal marker: consumers must decide
        on ``is_closed and queue.empty()`` (order-independent), never on having
        dequeued the sentinel (its FIFO position is arbitrary -- anything queued
        after close() sits BEHIND it). ``drain_for_handoff`` and
        ``await_next_method`` both discard it and keep going. It does not drive
        ``ctx.has_more_work()`` either: that reads ``queue.empty()`` then
        ``is_closed`` and never inspects the sentinel.

        This is advisory, not enforced: nothing gates producers on a CLOSED slot,
        so contributions CAN still arrive afterwards (see the auto-spawn callback
        in ExecutingWorkflow, which never reads is_closed; the one producer-side
        gate is ``orphaned``, which stashes instead). A closed slot holding
        only the sentinel reports has_more_work() True for one pass (the queue is
        non-empty until the wakeup is consumed).
        """
        if self.is_closed:
            return
        self.is_closed = True
        self.queue.put_nowait(SLOT_CLOSED_SENTINEL)

    def queue_empty(self) -> bool:
        return self.queue.empty()

    def undelivered_count(self) -> int:
        """Count queue + pending items that are neither sentinels nor completed.

        Queue inspection is pop-and-restore in original order: fully
        synchronous (no awaits), so it is atomic on the event loop; callers
        only invoke it when the sole consumer is dead or absent.
        """
        count = 0
        restore: list[SlotQueueItem] = []
        while not self.queue.empty():
            item = self.queue.get_nowait()
            restore.append(item)
            if isinstance(item, _SlotClosedSentinel):
                continue
            if item.completed.is_set():
                continue
            count += 1
        for item in restore:
            self.queue.put_nowait(item)
        for method in self.pending:
            if not method.completed.is_set():
                count += 1
        return count

    async def await_next_method(
        self, stop_event: asyncio.Event,
        waiter: IRegisteredThread | None = None,
    ) -> ExecutingMethod | None:
        """Wait for the next queued ``ExecutingMethod`` or a terminal condition.

        Returns ``None`` if the slot is closed and drained, or if
        ``stop_event`` fires before a method arrives. Otherwise returns
        the dequeued method.

        The asyncio.wait race between the queue get and the stop event
        is what lets a thread suspended at AWAITING_CO_THREADS honour
        ``thread.stop()`` immediately without first having to receive
        a queued method.

        Terminal only on ``is_closed and queue.empty()`` (order-independent) or
        stop_event. The close sentinel is a wakeup, not a verdict, and is
        discarded: work queued after close() sits behind it in the FIFO.

        Already-completed methods are skipped, symmetric with
        ``drain_for_handoff``: a still-looping receiver can dequeue a method its
        owner drove to completion while the receiver was busy elsewhere; binding
        that frozen action would double-bind it. (The orphaned-contribution case
        belongs to ``drain_for_handoff``, not here -- a receiver whose generator
        already exhausted never re-enters this function.)

        Await bookkeeping: records ``waiter`` in ``awaiting_threads`` and fires
        ``on_awaiting_join`` in the same synchronous step BEFORE any await, so
        closure evaluation observes the wait that completes an all-waiting
        state. The record is the CALLER's explicit identity: never inferred
        from ``active_thread``, which a colliding slot key or an injected
        recovery receiver can make wrong. Gated on an EMPTY queue: a receiver
        with unserved work in hand is one turn from executing, not purely
        reactive, so it must not be published as waiting -- it will re-enter
        with an empty queue after serving and be recorded then (a queue
        holding only the close sentinel exits via the top-of-loop test
        instead). Record + hook sit INSIDE the try so the ``finally`` removes
        the record even when the hook raises: a waiter that dies here must
        count as a live worker (fail-open), never stay recorded forever.
        Removal is by identity and touches only this caller's entry, so
        concurrent waiters never un-publish each other. Skipped when no
        ``waiter`` identity is supplied: the wait is engine-invisible by
        explicit contract.
        """
        recorded: IRegisteredThread | None = waiter if self.queue.empty() else None
        try:
            if recorded is not None:
                self.awaiting_threads.append(recorded)
                if self.on_awaiting_join is not None:
                    self.on_awaiting_join()
            while True:
                # A quarantined slot has no legitimate consumer: a late-arriving
                # receiver (operator-injected) must not dequeue the backlog.
                if self.orphaned:
                    return None
                if self.is_closed and self.queue.empty():
                    return None
                # Queued work is served first, same as a closed slot; whatever
                # is left over is re-routed by drain_for_handoff.
                if self.receiver_spent and self.queue.empty():
                    return None
                queue_task = asyncio.ensure_future(self.queue.get())
                stop_task = asyncio.ensure_future(stop_event.wait())
                done, pending = await asyncio.wait(
                    {queue_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await drain_cancelled_waiters(*pending)
                if stop_task in done:
                    return None
                result = queue_task.result()
                if isinstance(result, _SlotClosedSentinel):
                    # close() can land before a contribution is queued, leaving real
                    # work behind the sentinel in the FIFO; the top-of-loop test decides.
                    continue
                if result.completed.is_set():
                    continue
                return result
        finally:
            if recorded is not None:
                for i, t in enumerate(self.awaiting_threads):
                    if t is recorded:
                        del self.awaiting_threads[i]
                        break

    async def drain_for_handoff(
        self,
        callback: Callable[[str, ExecutingMethod, WorkflowRunMode], Awaitable[None]] | None,
        run_mode: WorkflowRunMode,
        completing_thread: LabwareThreadInstance | None = None,
    ) -> int:
        """Reset the slot for a fresh receiver and re-route undelivered methods.

        Called when the current active_thread completes. Drains pending
        (overflow stash) first, then ALL queue leftovers, re-firing the
        spawn callback for each item so it can route to a new receiver.
        SLOT_CLOSED_SENTINEL entries (if any got enqueued before handoff)
        are discarded -- close is a slot-level flag, not per-receiver.

        Methods that have already completed are skipped (no fresh-receiver
        spawn). The skip applies to BOTH the ``queue`` path (the
        single-yield-receiver-serves-two-methods race described below) AND
        the ``pending`` overflow deque path (an overflowed contributor that
        completed before drain runs).

        Race shape that motivated the skip: when the owner's auto-spawn
        binds method B to the active receiver while it is still processing
        method A, then method B's action body completes on the owner side
        because the receiver's labware happens to still be at the device.
        After the receiver's user generator exhausts (single yield), the
        bound method B sits in the queue but is already completed;
        re-routing it would spawn a spurious fresh receiver that visits
        no device and immediately exits its method loop (the original
        PLR ``test_plr_labware_journeys`` failure shape).

        Successor-receiver short-circuit: when ``completing_thread`` is
        supplied and the slot's current ``active_thread`` is a DIFFERENT
        thread, a successor receiver has already taken over (typical case:
        receiver R1 drained its user generator, the auto-spawn callback
        observed ``receiver_drained=True`` and minted R2 as the fresh
        receiver, then R1 finally reached completion). In that case the
        successor owns the slot's queue + pending; clearing active_thread
        here would orphan R2's bookkeeping and re-routing the slot's
        items would re-fire the spawn callback for methods R2 has already
        bound, minting a third spurious receiver that hangs forever in
        ``DispenseSpawn`` waiting for a stacker output that never empties
        (the pylabrobot E2E intermittent stall shape).

        Returns the number of items routed.
        """
        if (
            completing_thread is not None
            and self.active_thread is not None
            and self.active_thread.thread_instance is not completing_thread
        ):
            return 0
        self.active_thread = None
        self.contributions_to_active = 0
        self.receiver_drained = False
        items_to_route: list[ExecutingMethod] = []
        while self.pending:
            method = self.pending.popleft()
            if method.completed.is_set():
                continue
            items_to_route.append(method)
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if isinstance(item, _SlotClosedSentinel):
                continue
            if item.completed.is_set():
                continue
            items_to_route.append(item)
        if callback is not None:
            for method in items_to_route:
                await callback(self.labware_template_name, method, run_mode)
        return len(items_to_route)


class ILabwareRegistry(Protocol):
    def register(self, instance: LabwareInstance, template_name: str,
                 state: LabwareState, thread: IRegisteredThread | None = None) -> None: ...

    def update_state(self, instance_id: str, state: LabwareState) -> None: ...

    def update_thread(self, instance_id: str, thread: IRegisteredThread) -> None: ...

    def unregister(self, instance_id: str) -> None: ...

    def get_state(self, instance_id: str) -> LabwareState | None: ...

    def find_and_claim(self, template_name: str,
                       constraints: dict[str, str] | None = None) -> FindResult: ...

    def all_parked(self) -> list[RegisteredLabware]: ...

    def get_or_create_slot(self, slot_key: str, labware_template_name: str) -> LabwareSlot: ...

    def get_slot(self, slot_key: str) -> LabwareSlot | None: ...

    def slot_key_for(self, template: ThreadTemplate,
                     context: object | None = None) -> str: ...

    def slot_scope(self, slot_key: str) -> tuple[str | None, str | None]: ...

    def all_slots(self) -> dict[str, LabwareSlot]: ...


class InMemoryLabwareRegistry:
    """Ephemeral labware registry for a single workflow execution."""

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredLabware] = {}
        self._slots: dict[str, LabwareSlot] = {}

    def register(self, instance: LabwareInstance, template_name: str,
                 state: LabwareState, thread: IRegisteredThread | None = None) -> None:
        self._entries[instance.id] = RegisteredLabware(
            instance=instance,
            state=state,
            template_name=template_name,
            thread=thread,
        )

    def update_state(self, instance_id: str, state: LabwareState) -> None:
        entry = self._entries.get(instance_id)
        if entry is None:
            raise KeyError(f"Labware instance '{instance_id}' not registered")
        entry.state = state

    def unregister(self, instance_id: str) -> None:
        """Drop the entry for `instance_id`.

        Idempotent: silently no-ops if `instance_id` is absent so
        callers don't have to guard.
        """
        self._entries.pop(instance_id, None)

    def update_thread(self, instance_id: str, thread: IRegisteredThread) -> None:
        entry = self._entries.get(instance_id)
        if entry is None:
            raise KeyError(f"Labware instance '{instance_id}' not registered")
        entry.thread = thread

    def get_state(self, instance_id: str) -> LabwareState | None:
        entry = self._entries.get(instance_id)
        if entry is None:
            return None
        return entry.state

    def get_or_create_slot(self, slot_key: str, labware_template_name: str) -> LabwareSlot:
        if slot_key not in self._slots:
            self._slots[slot_key] = LabwareSlot(
                slot_key=slot_key,
                labware_template_name=labware_template_name,
            )
        return self._slots[slot_key]

    def get_slot(self, slot_key: str) -> LabwareSlot | None:
        return self._slots.get(slot_key)

    def slot_key_for(self, template: ThreadTemplate,
                     context: object | None = None) -> str:
        return template.labware_template.name

    def slot_scope(self, slot_key: str) -> tuple[str | None, str | None]:
        """Bare labware-name keys carry no group/submission scope (match any)."""
        return None, None

    def all_slots(self) -> dict[str, LabwareSlot]:
        return dict(self._slots)

    def find_and_claim(self, template_name: str,
                       constraints: dict[str, str] | None = None) -> FindResult:
        """Atomically find a PARKED instance and transition to IN_JOURNEY.

        This method is synchronous. The entire spawn callback must remain
        synchronous (no await) to guarantee atomicity under asyncio cooperative
        scheduling. This is a hard invariant.
        """
        parked = [e for e in self._entries.values()
                  if e.template_name == template_name and e.state == LabwareState.PARKED]

        if not parked:
            return NoneAvailable()

        if constraints is None:
            entry = parked[0]
            entry.state = LabwareState.IN_JOURNEY
            return Found(registered=entry)

        barcode = constraints.get("barcode")
        for entry in parked:
            if barcode is not None and entry.instance.barcode != barcode:
                continue
            entry.state = LabwareState.IN_JOURNEY
            return Found(registered=entry)

        return NoMatch(
            reason=f"No parked '{template_name}' matches constraints: {constraints}"
        )

    def all_parked(self) -> list[RegisteredLabware]:
        return [e for e in self._entries.values() if e.state == LabwareState.PARKED]
