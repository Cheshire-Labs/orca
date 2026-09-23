"""Unit tests for waiting-vs-executing worker discrimination in slot closure.

The worker-quiescence branch excludes only threads RECORDED as awaiting a join
on an open slot. The record is explicit caller identity
(``LabwareSlot.awaiting_threads``, appended on an empty-handed entry to
``await_next_method`` and removed on every exit): who is waiting is a fact the
waiter itself reports, never inferred from ``active_thread``. That keeps the
predicate sound for shapes where the caller is not the registered receiver
(colliding entry-thread slot keys, operator-injected recovery receivers) and
lets multiple concurrent waiters coexist.

Order-independence rules pinned here: the feeder sweep runs first and restarts
the pass on any close; the worker sweep decides exclusion-LIFTING slots (those
with recorded waiters) before slots that cannot lift, and restarts on its
FIRST close so later decisions see the rebuilt exclusion set.
"""
import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import PlateTemplate
from orca.resource_models.labware_state import IRegisteredThread, LabwareSlot
from orca.runtime.group_aware_labware_registry import GroupAwareLabwareRegistry
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow
from orca.workflow_models.workflows.workflow import WorkflowInstance


def _stub_receiver(alive: bool = True,
                   submission_id: str | None = "s1") -> IRegisteredThread:
    """Receiver stub; defaults to submission s1 so it is IN SCOPE for the
    test slots (an out-of-scope receiver holds nothing open by design)."""
    stub = SimpleNamespace(
        thread_instance=SimpleNamespace(
            thread_template=None, group_id=None, submission_id=submission_id,
        ),
        has_completed=lambda: not alive,
        has_finished_its_work=lambda: not alive,
    )
    return cast(IRegisteredThread, stub)


def _stub_thread(template_name: str, group_id: str | None,
                 submission_id: str | None, completed: bool) -> ExecutingLabwareThread:
    instance = SimpleNamespace(
        thread_template=SimpleNamespace(name=template_name),
        group_id=group_id,
        submission_id=submission_id,
    )
    stub = SimpleNamespace(
        thread_instance=instance,
        has_completed=lambda: completed,
        has_finished_its_work=lambda: completed,
    )
    return cast(ExecutingLabwareThread, stub)


def _dummy_thread(name: str, contributes_to: list[str]) -> ThreadTemplate:
    async def _fn(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        if False:
            yield  # pragma: no cover - never runs; satisfies async-generator typing
    return ThreadTemplate(
        labware_template=PlateTemplate(
            name, labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black"),
        start=f"{name}_pad",
        end=f"{name}_pad",
        func=_fn,
        contributes_to=contributes_to,
    )


def _executing_workflow(registry: GroupAwareLabwareRegistry,
                        template: WorkflowTemplate,
                        threads: list[ExecutingLabwareThread]) -> ExecutingWorkflow:
    wf = ExecutingWorkflow.__new__(ExecutingWorkflow)
    wf._labware_registry = registry
    wf._workflow = cast(WorkflowInstance, SimpleNamespace(template=template))
    wf._entry_threads = list(threads)
    wf._spawned_threads = []
    wf._pending_injections = 0
    return wf


def _slot(registry: GroupAwareLabwareRegistry, key: str) -> LabwareSlot:
    slot = registry.get_slot(key)
    assert slot is not None
    return slot


def _queued_method() -> MagicMock:
    method = MagicMock(spec=ExecutingMethod)
    # ``completed`` is set in __init__, so spec does not auto-provide it.
    method.completed = MagicMock()
    method.completed.is_set.return_value = False
    return method


def _both_roles_template() -> WorkflowTemplate:
    """feeder -> mid declared; pool has no feeders (worker-quiescence governs)."""
    wt = WorkflowTemplate("parked_predicate_test")
    wt.add_thread(_dummy_thread("feeder", ["mid"]))
    wt.add_thread(_dummy_thread("mid", []))
    wt.add_thread(_dummy_thread("pool", []))
    return wt


def _registry_with(slots: dict[str, tuple[str, IRegisteredThread | None, bool]],
                   order: list[str]) -> GroupAwareLabwareRegistry:
    """Create slots in a controlled order: key -> (template_name, receiver, waiting)."""
    registry = GroupAwareLabwareRegistry()
    for key in order:
        template_name, receiver, waiting = slots[key]
        slot = registry.get_or_create_slot(key, template_name)
        slot.active_thread = receiver
        if waiting and receiver is not None:
            slot.awaiting_threads.append(receiver)
    return registry


# --- LabwareSlot await bookkeeping (explicit caller identity) ---

class TestAwaitBookkeeping:
    @pytest.mark.asyncio
    async def test_recorded_while_waiting_and_removed_on_serve(self) -> None:
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        caller = _stub_receiver()
        slot.active_thread = caller
        waiter = asyncio.create_task(
            slot.await_next_method(asyncio.Event(), waiter=caller))
        await asyncio.sleep(0)
        assert slot.awaiting_threads == [caller]

        method = _queued_method()
        slot.queue.put_nowait(method)
        served = await asyncio.wait_for(waiter, timeout=2.0)

        assert served is method
        assert slot.awaiting_threads == []

    @pytest.mark.asyncio
    async def test_removed_on_stop_exit(self) -> None:
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        caller = _stub_receiver()
        slot.active_thread = caller
        stop = asyncio.Event()
        waiter = asyncio.create_task(
            slot.await_next_method(stop, waiter=caller))
        await asyncio.sleep(0)
        assert slot.awaiting_threads == [caller]

        stop.set()
        assert await asyncio.wait_for(waiter, timeout=2.0) is None
        assert slot.awaiting_threads == []

    @pytest.mark.asyncio
    async def test_removed_on_closed_and_empty_exit(self) -> None:
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        caller = _stub_receiver()
        slot.active_thread = caller
        waiter = asyncio.create_task(
            slot.await_next_method(asyncio.Event(), waiter=caller))
        await asyncio.sleep(0)
        assert slot.awaiting_threads == [caller]

        slot.close()

        assert await asyncio.wait_for(waiter, timeout=2.0) is None
        assert slot.awaiting_threads == []

    @pytest.mark.asyncio
    async def test_not_recorded_without_a_waiter_identity(self) -> None:
        """No identity supplied = nothing to record and no trigger: the wait is
        engine-invisible by explicit contract, not by accident."""
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        fired: list[bool] = []
        slot.on_awaiting_join = lambda: fired.append(True)
        stop = asyncio.Event()
        waiter = asyncio.create_task(slot.await_next_method(stop))
        await asyncio.sleep(0)
        assert slot.awaiting_threads == []
        assert fired == []

        stop.set()
        assert await asyncio.wait_for(waiter, timeout=2.0) is None

    @pytest.mark.asyncio
    async def test_wait_is_attributed_to_the_caller_not_active_thread(self) -> None:
        """The caller, not the registered receiver, is who is waiting: a
        colliding-key thread must never publish a wait on the receiver's
        behalf (that mis-attribution was the premature-close class)."""
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        registered = _stub_receiver()
        caller = _stub_receiver()
        slot.active_thread = registered
        stop = asyncio.Event()
        waiter = asyncio.create_task(
            slot.await_next_method(stop, waiter=caller))
        await asyncio.sleep(0)

        assert slot.awaiting_threads == [caller]
        assert registered not in slot.awaiting_threads

        stop.set()
        await asyncio.wait_for(waiter, timeout=2.0)
        assert slot.awaiting_threads == []

    @pytest.mark.asyncio
    async def test_two_waiters_recorded_and_removed_independently(self) -> None:
        """Concurrent waiters coexist: one exiting never un-publishes the
        other's genuine wait (a single bool could not carry two waiters)."""
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        a = _stub_receiver()
        b = _stub_receiver()
        slot.active_thread = a
        stop_a = asyncio.Event()
        stop_b = asyncio.Event()
        waiter_a = asyncio.create_task(slot.await_next_method(stop_a, waiter=a))
        waiter_b = asyncio.create_task(slot.await_next_method(stop_b, waiter=b))
        await asyncio.sleep(0)
        assert len(slot.awaiting_threads) == 2

        stop_a.set()
        await asyncio.wait_for(waiter_a, timeout=2.0)
        assert slot.awaiting_threads == [b]

        stop_b.set()
        await asyncio.wait_for(waiter_b, timeout=2.0)
        assert slot.awaiting_threads == []

    @pytest.mark.asyncio
    async def test_hook_fires_after_recording(self) -> None:
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        caller = _stub_receiver()
        slot.active_thread = caller
        observed: list[int] = []
        slot.on_awaiting_join = lambda: observed.append(len(slot.awaiting_threads))
        stop = asyncio.Event()
        waiter = asyncio.create_task(
            slot.await_next_method(stop, waiter=caller))
        await asyncio.sleep(0)

        assert observed == [1], (
            "on_awaiting_join must fire exactly once per entry, after the record"
        )
        stop.set()
        await asyncio.wait_for(waiter, timeout=2.0)

    @pytest.mark.asyncio
    async def test_queued_work_suppresses_the_recording(self) -> None:
        """A receiver with unserved work in hand is one turn from executing,
        not purely reactive; publishing it as waiting would let closure
        evaluation close slots its post-serve demand still needs."""
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        caller = _stub_receiver()
        slot.active_thread = caller
        observed: list[int] = []
        slot.on_awaiting_join = lambda: observed.append(len(slot.awaiting_threads))
        method = _queued_method()
        slot.queue.put_nowait(method)

        served = await asyncio.wait_for(
            slot.await_next_method(asyncio.Event(), waiter=caller), timeout=2.0)

        assert served is method
        assert observed == [], (
            "entry with work in hand must not be published as awaiting a join"
        )

    @pytest.mark.asyncio
    async def test_record_removed_when_the_hook_raises(self) -> None:
        """Fail-open: a waiter that dies in the hook must count as a live
        worker, never stay recorded as waiting forever."""
        slot = LabwareSlot(slot_key="pool:*:s1", labware_template_name="pool")
        caller = _stub_receiver()
        slot.active_thread = caller

        def _boom() -> None:
            raise RuntimeError("closure evaluation blew up")

        slot.on_awaiting_join = _boom
        with pytest.raises(RuntimeError):
            await slot.await_next_method(asyncio.Event(), waiter=caller)

        assert slot.awaiting_threads == []


# --- Closure evaluation: waiting-vs-executing discrimination ---

class TestWaitingWorkerPredicate:
    @pytest.mark.parametrize("order", [["mid:g2:s1", "pool:*:s1"],
                                       ["pool:*:s1", "mid:g2:s1"]])
    def test_executing_both_roles_thread_holds_sibling_slots_open(
            self, order: list[str]) -> None:
        """The core fix, in both slot-creation orders: an EXECUTING thread that
        is also a receiver counts as a worker, so the pool stays open; its own
        feeder-governed slot closes (feeder dead), which must not leak into the
        pool decision (feeder sweep first + restart)."""
        mid = _stub_thread("mid", "g2", "s1", completed=False)
        feeder = _stub_thread("feeder", "g2", "s1", completed=True)
        pool_receiver = _stub_receiver()
        registry = _registry_with(
            {
                "mid:g2:s1": ("mid", cast(IRegisteredThread, mid), False),
                "pool:*:s1": ("pool", pool_receiver, True),
            },
            order,
        )
        wf = _executing_workflow(registry, _both_roles_template(), [feeder, mid])

        wf._evaluate_slot_closures()

        assert _slot(registry, "mid:g2:s1").is_closed, "feeder-dead slot closes"
        assert not _slot(registry, "pool:*:s1").is_closed, (
            "pool must stay open: mid is alive and EXECUTING (not waiting), so "
            "it is a worker for the pool regardless of being a receiver elsewhere"
        )

    @pytest.mark.parametrize("order", [["a:*:s1", "b:*:s1"], ["b:*:s1", "a:*:s1"]])
    def test_worker_close_restarts_before_sibling_decision(
            self, order: list[str]) -> None:
        """Two waiting receivers of two open no-feeder slots, nothing else live:
        exactly ONE slot closes per evaluation. Its receiver then counts as a
        worker (waiting on a CLOSED slot wakes and may yield demand), holding
        the sibling open until the wake cascades."""
        r_a = _stub_receiver()
        r_b = _stub_receiver()
        wt = WorkflowTemplate("cascade_test")
        wt.add_thread(_dummy_thread("a", []))
        wt.add_thread(_dummy_thread("b", []))
        registry = _registry_with(
            {
                "a:*:s1": ("a", r_a, True),
                "b:*:s1": ("b", r_b, True),
            },
            order,
        )
        wf = _executing_workflow(
            registry, wt,
            [cast(ExecutingLabwareThread, r_a), cast(ExecutingLabwareThread, r_b)],
        )

        wf._evaluate_slot_closures()

        closed = [k for k in ("a:*:s1", "b:*:s1") if _slot(registry, k).is_closed]
        assert len(closed) == 1, (
            f"exactly one of the mutually-waiting slots closes per evaluation; "
            f"the wake cascade closes the rest. closed={closed}"
        )

    @pytest.mark.parametrize("order", [["tips:*:s1", "pool:*:s1"],
                                       ["pool:*:s1", "tips:*:s1"]])
    def test_asymmetric_orphan_slot_never_steals_the_lift(
            self, order: list[str]) -> None:
        """An open slot with NO receiver (a drained rack) next to a slot with a
        waiting receiver: the waiting-receiver slot closes, and its receiver's
        lifted exclusion holds the orphan slot OPEN, in BOTH creation orders."""
        receiver = _stub_receiver()
        wt = WorkflowTemplate("asym_order_test")
        wt.add_thread(_dummy_thread("tips", []))
        wt.add_thread(_dummy_thread("pool", []))
        registry = _registry_with(
            {
                "tips:*:s1": ("tips", None, False),
                "pool:*:s1": ("pool", receiver, True),
            },
            order,
        )
        wf = _executing_workflow(
            registry, wt, [cast(ExecutingLabwareThread, receiver)],
        )

        wf._evaluate_slot_closures()

        assert _slot(registry, "pool:*:s1").is_closed, (
            "the waiting-receiver slot closes: nothing can ever feed it"
        )
        assert not _slot(registry, "tips:*:s1").is_closed, (
            "the orphan slot must stay open: pool's receiver wakes on its own "
            "close and may yield post-loop demand for tips"
        )

    @pytest.mark.parametrize("order", [
        ["a:*:s1", "b:*:s1", "tips:*:s1"],
        ["tips:*:s1", "b:*:s1", "a:*:s1"],
        ["b:*:s1", "tips:*:s1", "a:*:s1"],
    ])
    def test_two_lift_slots_and_an_orphan(self, order: list[str]) -> None:
        """Two waiting-receiver slots plus an orphan: exactly one lift slot
        closes per evaluation and the orphan stays open in every creation
        order (the surviving waiter and the woken one both count as workers)."""
        r_a = _stub_receiver()
        r_b = _stub_receiver()
        wt = WorkflowTemplate("two_lift_orphan_test")
        wt.add_thread(_dummy_thread("a", []))
        wt.add_thread(_dummy_thread("b", []))
        wt.add_thread(_dummy_thread("tips", []))
        registry = _registry_with(
            {
                "a:*:s1": ("a", r_a, True),
                "b:*:s1": ("b", r_b, True),
                "tips:*:s1": ("tips", None, False),
            },
            order,
        )
        wf = _executing_workflow(
            registry, wt,
            [cast(ExecutingLabwareThread, r_a), cast(ExecutingLabwareThread, r_b)],
        )

        wf._evaluate_slot_closures()

        closed = [k for k in ("a:*:s1", "b:*:s1") if _slot(registry, k).is_closed]
        assert len(closed) == 1, f"exactly one lift slot closes; closed={closed}"
        assert not _slot(registry, "tips:*:s1").is_closed, (
            "the orphan must stay open in every order"
        )

    def test_out_of_scope_waiter_neither_holds_nor_is_held(self) -> None:
        """A waiting receiver from another submission neither holds a sibling
        scope's slot open nor blocks its own slot's close: scope boundaries
        apply to waiters exactly as to workers."""
        foreign_waiter = _stub_receiver(submission_id="s2")
        wt = WorkflowTemplate("scope_waiter_test")
        wt.add_thread(_dummy_thread("pool", []))
        wt.add_thread(_dummy_thread("tips", []))
        registry = _registry_with(
            {
                "pool:*:s2": ("pool", foreign_waiter, True),
                "tips:*:s1": ("tips", None, False),
            },
            ["pool:*:s2", "tips:*:s1"],
        )
        s1_worker = _stub_thread("feeder", "g1", "s1", completed=True)
        wf = _executing_workflow(
            registry, wt,
            [cast(ExecutingLabwareThread, foreign_waiter), s1_worker],
        )

        wf._evaluate_slot_closures()

        assert _slot(registry, "tips:*:s1").is_closed, (
            "the s1 slot closes on its own scope's quiescence; the s2 waiter "
            "must not hold it open"
        )
        assert _slot(registry, "pool:*:s2").is_closed, (
            "the s2 slot closes too: its only in-scope thread is waiting on it"
        )

    def test_all_waiting_or_done_closes_the_no_feeder_slot(self) -> None:
        """Termination guarantee: once every in-scope thread is waiting or done,
        the worker-quiesced close fires."""
        pool_receiver = _stub_receiver()
        feeder = _stub_thread("feeder", "g1", "s1", completed=True)
        wt = _both_roles_template()
        registry = _registry_with(
            {"pool:*:s1": ("pool", pool_receiver, True)}, ["pool:*:s1"],
        )
        wf = _executing_workflow(registry, wt, [feeder])

        wf._evaluate_slot_closures()

        assert _slot(registry, "pool:*:s1").is_closed

    @pytest.mark.asyncio
    async def test_await_trigger_closes_own_slot_when_it_completes_quiescence(
            self) -> None:
        """A receiver whose wait completes the all-waiting state must be closed
        out by its own transition: no thread terminal fires at that moment."""
        wt = _both_roles_template()
        registry = GroupAwareLabwareRegistry()
        slot = registry.get_or_create_slot("pool:*:s1", "pool")
        receiver = _stub_receiver()
        slot.active_thread = receiver
        feeder = _stub_thread("feeder", "g1", "s1", completed=True)
        # The receiver is a workflow thread too (production adds receivers via
        # add_thread); without it the pre-wait state has no live worker at all.
        wf = _executing_workflow(
            registry, wt, [feeder, cast(ExecutingLabwareThread, receiver)],
        )
        slot.on_awaiting_join = wf._on_receiver_awaiting_join

        # Pre-wait, the receiver itself is a live non-waiting worker: no close.
        wf._evaluate_slot_closures()
        assert not slot.is_closed

        result = await asyncio.wait_for(
            slot.await_next_method(asyncio.Event(), waiter=receiver), timeout=2.0,
        )

        assert result is None, "the await trigger must close the slot mid-wait"
        assert slot.is_closed

    @pytest.mark.asyncio
    async def test_invisible_waiter_shape_is_now_visible(self) -> None:
        """An injected recovery receiver never becomes active_thread; with
        explicit caller identity its wait is still recorded, the trigger fires,
        and the all-waiting close reaches it (previously it waited invisibly
        and the workflow hung open)."""
        wt = _both_roles_template()
        registry = GroupAwareLabwareRegistry()
        slot = registry.get_or_create_slot("pool:*:s1", "pool")
        recovery = _stub_receiver()
        assert slot.active_thread is None
        feeder = _stub_thread("feeder", "g1", "s1", completed=True)
        wf = _executing_workflow(
            registry, wt, [feeder, cast(ExecutingLabwareThread, recovery)],
        )
        slot.on_awaiting_join = wf._on_receiver_awaiting_join

        result = await asyncio.wait_for(
            slot.await_next_method(asyncio.Event(), waiter=recovery), timeout=2.0,
        )

        assert result is None, (
            "the recovery waiter must be visible to closure evaluation and be "
            "closed out on all-waiting quiescence, not wait forever"
        )
        assert slot.is_closed
