"""Tests for the spawn callback in ExecutingWorkflow._make_auto_spawn_callback().

These tests construct a real ExecutingWorkflow (with mocked heavy deps) and
exercise the closure stored at executing_workflow._auto_spawn_callback.

Covers:
- T1: full slot stashes overflow in pending (via SequentialStashStrategy)
- T2: REJECT policy raises CapacityExceededError on overflow
- T3: depleted labware (can_continue=False) triggers overflow even when slot has room
- T6: a custom LabwareInstance subclass's can_continue() override is consulted
- T7: a custom IOverflowStrategy registered via wf.thread() receives slot/method/workflow
"""
import asyncio
from typing import Callable, cast
from unittest.mock import MagicMock

import pytest

from orca.events.event_bus import EventBus
from orca.events.execution_context import ExecutionContext
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import ResolvedAcquisition
from orca.resource_models.capacity import (
    CapacityExceededError,
    RecoverableCapacityExceededError,
    CapacityPolicy,
    OverflowAction,
)
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.state.records import DeclaredTracking
from orca.resource_models.labware_state import (
    SLOT_CLOSED_SENTINEL,
    InMemoryLabwareRegistry,
    IRegisteredThread,
    IWorkflowRef,
    LabwareSlot,
)
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.overflow_strategy import IOverflowStrategy
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.executing_workflow import (
    CreateThreadFn,
    ExecutingWorkflow,
)
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _ControlledLabware(LabwareInstance):
    """LabwareInstance subclass with a flippable can_continue() result."""

    def __init__(self, name: str, can_continue_value: bool = True) -> None:
        super().__init__(name, "test_type")
        self.can_continue_value = can_continue_value
        self.can_continue_call_count = 0

    async def can_continue(self, demand: DeclaredTracking | None = None) -> bool:
        self.can_continue_call_count += 1
        return self.can_continue_value


class _StubLabwareTemplate(LabwareTemplate):
    """Minimal LabwareTemplate that yields _ControlledLabware instances."""

    def __init__(self, name: str, labware: LabwareInstance) -> None:
        super().__init__(name)
        self._instance = labware

    async def create_instance(self) -> LabwareInstance:
        return self._instance


class _StubRegisteredThread:
    """IRegisteredThread test double with controllable labware + completion."""

    def __init__(self, labware: LabwareInstance, labware_template: LabwareTemplate,
                 thread_instance: LabwareThreadInstance) -> None:
        self._labware_template = labware_template
        self._thread_instance = thread_instance
        self.completed = False

    def has_completed(self) -> bool:
        return self.completed

    def stop(self) -> None:
        self.completed = True

    @property
    def labware_template(self) -> LabwareTemplate | None:
        return self._labware_template

    @property
    def thread_instance(self) -> LabwareThreadInstance:
        return self._thread_instance


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Harness:
    """Bundles the workflow, registry, slot, callback, and the active receiver."""

    def __init__(self,
                 workflow: ExecutingWorkflow,
                 registry: InMemoryLabwareRegistry,
                 receiver_template: ThreadTemplate,
                 receiver_labware: LabwareInstance,
                 active_thread: _StubRegisteredThread,
                 callback: Callable[[str, ExecutingMethod], None],
                 event_bus: EventBus) -> None:
        self.workflow = workflow
        self.registry = registry
        self.receiver_template = receiver_template
        self.receiver_labware = receiver_labware
        self.active_thread = active_thread
        self.callback = callback
        self.event_bus = event_bus

    @property
    def slot(self) -> LabwareSlot:
        slot = self.registry.get_slot(self.receiver_template.labware_template.name)
        assert slot is not None, "Slot was not created"
        return slot


def _build_harness(
    receiver_labware: LabwareInstance,
    capacity: CapacityPolicy | None = None,
    overflow_strategy: object | None = None,
) -> _Harness:
    """Construct an ExecutingWorkflow + pre-populate a slot with an active receiver.

    Mocks all heavy deps (reservation coordinator, thread manager, move handler,
    status manager, system map). We only exercise the active-receiver branch of
    the spawn callback, which doesn't need them.
    """
    receiver_labware_template = _StubLabwareTemplate(
        receiver_labware.template_name, receiver_labware,
    )
    receiver_thread_template = MagicMock(spec=ThreadTemplate)
    receiver_thread_template.name = f"{receiver_labware.template_name}_thread"
    receiver_thread_template.labware_template = receiver_labware_template

    template = WorkflowTemplate(name="test_workflow")
    template.register_auto_spawn(
        receiver_thread_template,
        capacity=capacity,
        overflow_strategy=overflow_strategy,
    )

    workflow_instance = WorkflowInstance(name="test_workflow", template=template)
    registry = InMemoryLabwareRegistry()
    event_bus = EventBus()

    create_thread_fn = cast(CreateThreadFn, MagicMock())

    workflow = ExecutingWorkflow(
        workflow=workflow_instance,
        thread_reservation_coordinator=MagicMock(),
        system_thread_manager=MagicMock(),
        event_bus=event_bus,
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        system_map=MagicMock(),
        create_thread_fn=create_thread_fn,
        labware_registry=registry,
    )

    callback = workflow._auto_spawn_callback
    assert callback is not None, "callback was None; check create_thread_fn / template"

    # Pre-populate slot with an active receiver. This bypasses the
    # create-fresh-receiver branch (which is exercised by integration tests).
    receiver_thread_instance = MagicMock(spec=LabwareThreadInstance)
    receiver_thread_instance.labware = receiver_labware

    active_thread = _StubRegisteredThread(
        labware=receiver_labware,
        labware_template=receiver_labware_template,
        thread_instance=receiver_thread_instance,
    )
    slot = registry.get_or_create_slot(
        receiver_labware_template.name, receiver_labware_template.name,
    )
    slot.active_thread = active_thread
    slot.policy = capacity
    slot.overflow_strategy = overflow_strategy

    return _Harness(
        workflow=workflow,
        registry=registry,
        receiver_template=receiver_thread_template,
        receiver_labware=receiver_labware,
        active_thread=active_thread,
        callback=callback,
        event_bus=event_bus,
    )


def _make_method(name: str) -> ExecutingMethod:
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    return method


def _capture_events(event_bus: EventBus) -> list[tuple[str, ExecutionContext]]:
    captured: list[tuple[str, ExecutionContext]] = []
    event_bus.subscribe_all(lambda name, ctx: captured.append((name, ctx)))
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSpawnCallbackOverflow:

    @pytest.mark.asyncio
    async def test_full_slot_stashes_overflow_in_pending(self) -> None:
        """T1: max=2, NEW. After 3 spawns: queue=2, pending=1, OVERFLOW emitted."""
        labware = _ControlledLabware("plate_recv")
        harness = _build_harness(
            receiver_labware=labware,
            capacity=CapacityPolicy(max_contributions=2,
                                    overflow_action=OverflowAction.NEW),
        )
        captured = _capture_events(harness.event_bus)

        for i in range(3):
            await harness.callback(harness.receiver_template.labware_template.name,
                                   _make_method(f"m{i}"),
                                   WorkflowRunMode.PURE_SIM)

        assert harness.slot.queue.qsize() == 2
        assert len(harness.slot.pending) == 1
        assert harness.slot.contributions_to_active == 2
        overflow_events = [n for n, _ in captured if n.endswith(".OVERFLOW")]
        assert len(overflow_events) == 1
        assert overflow_events[0] == "SLOT.plate_recv.OVERFLOW"

    @pytest.mark.asyncio
    async def test_reject_policy_raises_on_overflow(self) -> None:
        """T2: max=2, REJECT. 3rd call raises CapacityExceededError; REJECTED emitted."""
        labware = _ControlledLabware("plate_recv")
        harness = _build_harness(
            receiver_labware=labware,
            capacity=CapacityPolicy(max_contributions=2,
                                    overflow_action=OverflowAction.REJECT),
        )
        captured = _capture_events(harness.event_bus)

        # First 2 fill the slot
        await harness.callback("plate_recv", _make_method("m0"), WorkflowRunMode.PURE_SIM)
        await harness.callback("plate_recv", _make_method("m1"), WorkflowRunMode.PURE_SIM)

        # 3rd raises
        with pytest.raises(CapacityExceededError, match="plate_recv"):
            await harness.callback("plate_recv", _make_method("m2"), WorkflowRunMode.PURE_SIM)

        # State after rejection: 2 in queue, 0 pending
        assert harness.slot.queue.qsize() == 2
        assert len(harness.slot.pending) == 0
        rejected_events = [n for n, _ in captured if n.endswith(".REJECTED")]
        assert len(rejected_events) == 1
        assert rejected_events[0] == "SLOT.plate_recv.REJECTED"

    @pytest.mark.asyncio
    async def test_depleted_labware_triggers_overflow_even_with_slot_room(self) -> None:
        """T3: no slot policy (unlimited room) but labware can_continue()=False.
        Overflow must still fire because labware-level capacity precedes slot-level."""
        labware = _ControlledLabware("plate_recv", can_continue_value=False)
        harness = _build_harness(
            receiver_labware=labware,
            capacity=None,  # slot is unbounded -- only labware capacity can trigger overflow
        )

        await harness.callback("plate_recv", _make_method("m0"), WorkflowRunMode.PURE_SIM)

        # No WORK enqueued. The queue holds only the wake sentinel that
        # mark_receiver_spent puts there to lift a receiver already parked in
        # await_next_method; consumers discard it, exactly as with close().
        queued = [
            harness.slot.queue.get_nowait()
            for _ in range(harness.slot.queue.qsize())
        ]
        assert all(item is SLOT_CLOSED_SENTINEL for item in queued), (
            f"the overflowed method must not reach the spent receiver: {queued}"
        )
        assert harness.slot.receiver_spent is True
        assert len(harness.slot.pending) == 1   # routed to overflow strategy
        assert harness.slot.contributions_to_active == 0

    @pytest.mark.asyncio
    async def test_recoverable_reject_still_records_the_depletion(self) -> None:
        """The pre-check raises before the commit callback ever runs.

        Under RECOVERABLE_REJECT that makes the pre-check the only place a
        depleted receiver could hear it is spent, so it has to say so. Without
        that the contributor pauses, the operator retries, the pre-check finds
        the same depleted labware and raises again, forever.
        """
        labware = _ControlledLabware("plate_recv", can_continue_value=False)
        harness = _build_harness(
            receiver_labware=labware,
            capacity=CapacityPolicy(
                max_contributions=4,
                overflow_action=OverflowAction.RECOVERABLE_REJECT,
            ),
        )
        precheck = harness.workflow._capacity_precheck_callback
        assert precheck is not None

        with pytest.raises(RecoverableCapacityExceededError):
            await precheck("plate_recv", None, _make_method("m0"))

        assert harness.slot.receiver_spent is True, (
            "a depleted receiver under RECOVERABLE_REJECT never learns it is "
            "spent, so the operator's retry hits the same wall"
        )

    @pytest.mark.asyncio
    async def test_custom_labware_can_continue_override_is_called(self) -> None:
        """T6: subclass override is consulted, not the base class default.

        Confirms that can_continue() is dispatched dynamically. If the spawn
        callback bypassed the override and used the base class default (always True),
        the count below would be 0 and no overflow would occur.
        """
        labware = _ControlledLabware("plate_recv", can_continue_value=False)
        harness = _build_harness(receiver_labware=labware, capacity=None)

        await harness.callback("plate_recv", _make_method("m0"), WorkflowRunMode.PURE_SIM)

        assert labware.can_continue_call_count >= 1
        # And the override's False return triggered overflow:
        assert len(harness.slot.pending) == 1


class TestSpawnCallbackCustomStrategy:

    @pytest.mark.asyncio
    async def test_custom_overflow_strategy_receives_slot_method_workflow(self) -> None:
        """T7: a strategy registered via wf.thread(overflow_strategy=...) is invoked
        with the right slot, method, and workflow ref on overflow."""
        recorded: list[tuple[LabwareSlot, ExecutingMethod, IWorkflowRef]] = []

        class RecordingStrategy:
            def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                            workflow: IWorkflowRef) -> None:
                recorded.append((slot, method, workflow))

        custom = RecordingStrategy()
        labware = _ControlledLabware("plate_recv")
        harness = _build_harness(
            receiver_labware=labware,
            capacity=CapacityPolicy(max_contributions=1,
                                    overflow_action=OverflowAction.NEW),
            overflow_strategy=custom,
        )

        # First fills the slot, second triggers overflow
        first = _make_method("m0")
        second = _make_method("m1")
        await harness.callback("plate_recv", first, WorkflowRunMode.PURE_SIM)
        await harness.callback("plate_recv", second, WorkflowRunMode.PURE_SIM)

        assert len(recorded) == 1
        recorded_slot, recorded_method, recorded_workflow = recorded[0]
        assert recorded_slot is harness.slot
        assert recorded_method is second
        # workflow ref carries the right workflow id
        assert recorded_workflow.id == harness.workflow.id
        # Custom strategy did NOT auto-stash to pending
        assert len(harness.slot.pending) == 0

    def test_custom_strategy_satisfies_protocol(self) -> None:
        """A class with an on_overflow method satisfies IOverflowStrategy."""
        class GoodStrategy:
            def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                            workflow: object) -> None:
                pass

        assert isinstance(GoodStrategy(), IOverflowStrategy)


# ---------------------------------------------------------------------------
# Concurrent create-fresh-receiver race
# ---------------------------------------------------------------------------

async def _poll(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    """Yield the event loop until predicate() holds or timeout elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.001)
    return True


def _build_concurrent_create_harness() -> tuple[
    ExecutingWorkflow, str, list[int], asyncio.Event
]:
    """ExecutingWorkflow whose slot starts EMPTY (no active receiver), with a
    create_thread_fn that blocks on its FIRST call so a second contributor can
    interleave the check-then-create window. Returns
    (workflow, labware_name, create_calls, release_event).
    """
    receiver_labware = _ControlledLabware("final_recv")
    receiver_labware_template = _StubLabwareTemplate("final_recv", receiver_labware)
    receiver_thread_template = MagicMock(spec=ThreadTemplate)
    receiver_thread_template.name = "final_recv_thread"
    receiver_thread_template.labware_template = receiver_labware_template
    receiver_thread_template.start_reuse_existing = False

    template = WorkflowTemplate(name="test_workflow")
    template.register_auto_spawn(
        receiver_thread_template, capacity=None, overflow_strategy=None,
    )
    workflow_instance = WorkflowInstance(name="test_workflow", template=template)
    registry = InMemoryLabwareRegistry()

    create_calls: list[int] = []
    release = asyncio.Event()

    async def create_fn(
        _template: ThreadTemplate,
        _method: ExecutingMethod | None,
        _resolved: ResolvedAcquisition | None,
        _run_mode: WorkflowRunMode,
    ) -> LabwareThreadInstance:
        idx = len(create_calls)
        create_calls.append(idx)
        if idx == 0:
            await release.wait()
        thread_instance = MagicMock(spec=LabwareThreadInstance)
        thread_instance.id = f"final_recv-{idx}"
        return thread_instance

    # A minted receiver must read back as ACTIVE (has_completed=False) so the
    # second contributor binds to it; manual_start=True skips the real task start.
    created = MagicMock()
    created.has_completed.return_value = False
    created.manual_start = True
    created.thread_instance.labware = receiver_labware
    created.labware_template = receiver_labware_template
    thread_manager = MagicMock()
    thread_manager.create_executing_thread.return_value = created

    workflow = ExecutingWorkflow(
        workflow=workflow_instance,
        thread_reservation_coordinator=MagicMock(),
        system_thread_manager=thread_manager,
        event_bus=EventBus(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        system_map=MagicMock(),
        create_thread_fn=cast(CreateThreadFn, create_fn),
        labware_registry=registry,
    )
    return workflow, receiver_labware_template.name, create_calls, release


def _contributor_method(name: str) -> ExecutingMethod:
    """ExecutingMethod double with contributor_context=None (base registry keys
    by bare labware name, so both contributors resolve to the same slot)."""
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    method.contributor_context = None
    return cast(ExecutingMethod, method)


class TestSpawnCallbackConcurrentCreate:
    """Two contributors firing into one EMPTY shared slot must collapse onto a
    single receiver, not each mint their own. This is the JOIN_EXISTING
    multi-plate batching guarantee, and the regression guard for the
    concurrent check-then-create race that LabwareSlot.spawn_lock closes.
    """

    @pytest.mark.asyncio
    async def test_concurrent_contributors_spawn_one_receiver(self) -> None:
        workflow, name, create_calls, release = _build_concurrent_create_harness()
        callback = workflow._auto_spawn_callback
        assert callback is not None

        method_a = _contributor_method("m_a")
        method_b = _contributor_method("m_b")

        task_a = asyncio.ensure_future(
            callback(name, method_a, WorkflowRunMode.PURE_SIM)
        )
        # A is now parked inside create_thread_fn, holding the slot lock.
        assert await _poll(lambda: len(create_calls) >= 1), "A never reached create"

        task_b = asyncio.ensure_future(
            callback(name, method_b, WorkflowRunMode.PURE_SIM)
        )
        # Give B a window to either re-create (unlocked: 2nd create) or block on
        # the slot lock (locked: no 2nd create). Timing out here IS the pass path.
        await _poll(lambda: len(create_calls) >= 2, timeout=0.5)

        release.set()
        await asyncio.gather(task_a, task_b)

        assert len(create_calls) == 1, (
            f"Concurrent contributors to one slot must collapse onto a single "
            f"receiver; create_thread_fn ran {len(create_calls)}x"
        )


class TestSpawnCallbackContributionIndex:
    """Each contribution is stamped with its 0-based index on the same receiver
    instance, so the writing action can route contributor N to region N. The
    first contribution (minting the receiver) is 0; each subsequent one increments.
    """

    @pytest.mark.asyncio
    async def test_active_branch_stamps_incrementing_index(self) -> None:
        labware = _ControlledLabware("plate_recv")
        harness = _build_harness(receiver_labware=labware, capacity=None)

        methods = [_make_method(f"m{i}") for i in range(3)]
        for method in methods:
            await harness.callback("plate_recv", method, WorkflowRunMode.PURE_SIM)

        methods[0].set_pool_index.assert_called_once_with("plate_recv", 0)
        methods[1].set_pool_index.assert_called_once_with("plate_recv", 1)
        methods[2].set_pool_index.assert_called_once_with("plate_recv", 2)

    @pytest.mark.asyncio
    async def test_minted_receiver_is_index_zero(self) -> None:
        workflow, name, create_calls, release = _build_concurrent_create_harness()
        callback = workflow._auto_spawn_callback
        assert callback is not None

        method = _contributor_method("m0")
        release.set()  # single contributor: mint immediately, don't park in create_fn
        await callback(name, method, WorkflowRunMode.PURE_SIM)

        assert len(create_calls) == 1
        method.set_pool_index.assert_called_once_with(name, 0)
