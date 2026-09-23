"""Tests for LabwareSlot.drain_for_handoff().

When the active receiver of a slot completes, the runtime calls
slot.drain_for_handoff(callback). This:
1. Resets active_thread to None and contributions_to_active to 0.
2. Drains slot.pending (overflow stash) in FIFO order.
3. Drains ALL items from slot.queue (the bug-fix this test guards is that
   the original code drained only one queue item; the fixed code drains all).
4. Re-fires the callback for each routed item so a fresh receiver is created
   (or another overflow strategy fires, depending on the slot policy).

Covers:
- T4: pending items are drained and re-routed via the callback in FIFO order
- T5: ALL queue leftovers are drained (the reviewer-flagged bug)
- Bonus: pending drains BEFORE queue (deterministic ordering)
- Bonus: drain is a no-op when both pending and queue are empty
- Bonus: drain leaves slot in a clean state (active_thread=None, contributions=0)
"""
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

import pytest

from orca.resource_models.capacity import CapacityPolicy, OverflowAction
from orca.resource_models.labware_state import LabwareSlot
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.method import ExecutingMethod


def _make_method(name: str) -> ExecutingMethod:
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    # ``ExecutingMethod.completed`` is an asyncio.Event set in __init__,
    # so spec=ExecutingMethod does NOT auto-provide it. drain_for_handoff
    # consults ``.completed.is_set()`` to skip already-completed methods;
    # set it to a mock returning False so test methods are routed normally.
    method.completed = MagicMock()
    method.completed.is_set.return_value = False
    return method


def _record_callback() -> tuple[
    list[tuple[str, ExecutingMethod]],
    Callable[[str, ExecutingMethod], Awaitable[None]],
]:
    calls: list[tuple[str, ExecutingMethod]] = []

    async def callback(slot_key: str, method: ExecutingMethod, run_mode: WorkflowRunMode) -> None:
        calls.append((slot_key, method))

    return calls, callback


class TestDrainPending:
    """T4: pending items get re-routed via callback in FIFO order."""

    @pytest.mark.asyncio
    async def test_drains_pending_and_refires_callback_in_fifo_order(self) -> None:
        slot = LabwareSlot(slot_key="plate_x", labware_template_name="plate_x")
        m1, m2, m3 = _make_method("m1"), _make_method("m2"), _make_method("m3")
        slot.pending.extend([m1, m2, m3])

        calls, callback = _record_callback()
        routed_count = await slot.drain_for_handoff(callback, WorkflowRunMode.PURE_SIM)

        assert routed_count == 3
        assert len(calls) == 3
        assert [c[0] for c in calls] == ["plate_x", "plate_x", "plate_x"]
        assert [c[1] for c in calls] == [m1, m2, m3]
        assert len(slot.pending) == 0


class TestDrainAllQueueLeftovers:
    """T5: the reviewer-flagged bug -- the drain must pop ALL queue items,
    not just one. With a buggy `if not empty` instead of `while not empty`,
    only the first item is re-routed and the rest are silently lost."""

    @pytest.mark.asyncio
    async def test_drains_all_queue_items_not_just_one(self) -> None:
        slot = LabwareSlot(
            slot_key="plate_y",
            labware_template_name="plate_y",
            policy=CapacityPolicy(max_contributions=4,
                                  overflow_action=OverflowAction.NEW),
        )
        # Simulate 4 items having been enqueued via the spawn callback,
        # then the receiver exiting before consuming any of them
        for i in range(4):
            slot.queue.put_nowait(_make_method(f"q{i}"))
        slot.contributions_to_active = 4  # would have been incremented by callback

        calls, callback = _record_callback()
        routed_count = await slot.drain_for_handoff(callback, WorkflowRunMode.PURE_SIM)

        assert routed_count == 4
        assert len(calls) == 4
        assert [c[1].name for c in calls] == ["q0", "q1", "q2", "q3"]
        assert slot.queue.empty()


class TestDrainOrdering:
    """Pending must drain BEFORE queue. SequentialStashStrategy stashed pending
    after the queue filled, so pending items are conceptually earlier in
    arrival order and must be re-routed first."""

    @pytest.mark.asyncio
    async def test_pending_drains_before_queue(self) -> None:
        slot = LabwareSlot(slot_key="plate_z", labware_template_name="plate_z")
        # Pending was filled while queue was full, but pending items arrived
        # at the spawn callback FIRST in real time
        p1, p2 = _make_method("p1"), _make_method("p2")
        q1, q2 = _make_method("q1"), _make_method("q2")
        slot.pending.extend([p1, p2])
        slot.queue.put_nowait(q1)
        slot.queue.put_nowait(q2)

        calls, callback = _record_callback()
        await slot.drain_for_handoff(callback, WorkflowRunMode.PURE_SIM)

        names = [c[1].name for c in calls]
        assert names == ["p1", "p2", "q1", "q2"]


class TestDrainResetState:
    """Drain leaves the slot ready for a new receiver: active_thread=None,
    contributions_to_active=0. The next callback fire on the empty slot
    will hit the no-active-thread branch and create a fresh receiver."""

    @pytest.mark.asyncio
    async def test_drain_clears_active_thread_and_contributions(self) -> None:
        slot = LabwareSlot(
            slot_key="plate_x",
            labware_template_name="plate_x",
            policy=CapacityPolicy(max_contributions=2),
        )
        slot.active_thread = MagicMock()
        slot.contributions_to_active = 2
        # No pending or queue items

        calls, callback = _record_callback()
        routed = await slot.drain_for_handoff(callback, WorkflowRunMode.PURE_SIM)

        assert routed == 0
        assert len(calls) == 0
        assert slot.active_thread is None
        assert slot.contributions_to_active == 0


class TestDrainNoOpCases:
    """Drain on an empty slot does nothing observable beyond resetting state."""

    @pytest.mark.asyncio
    async def test_drain_with_no_items_does_not_call_callback(self) -> None:
        slot = LabwareSlot(slot_key="plate_x", labware_template_name="plate_x")

        calls, callback = _record_callback()
        routed = await slot.drain_for_handoff(callback, WorkflowRunMode.PURE_SIM)

        assert routed == 0
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_drain_with_none_callback_still_clears_items(self) -> None:
        """If the callback is None, items are still drained from the slot
        (preventing accumulated state on subsequent fires) -- they just
        cannot be re-routed."""
        slot = LabwareSlot(slot_key="plate_x", labware_template_name="plate_x")
        slot.pending.append(_make_method("p1"))
        slot.queue.put_nowait(_make_method("q1"))

        routed = await slot.drain_for_handoff(None, WorkflowRunMode.PURE_SIM)

        assert routed == 2
        assert len(slot.pending) == 0
        assert slot.queue.empty()
