"""Regression tests for the close-sentinel-as-wakeup contract.

close() marks the slot and enqueues SLOT_CLOSED_SENTINEL purely to wake a
receiver parked in queue.get(). The sentinel's FIFO position is arbitrary:
the auto-spawn callback never reads is_closed, so a contribution can be
queued AFTER close() and land BEHIND the sentinel. await_next_method must
therefore treat the sentinel as a wakeup and decide only on the
order-independent test ``is_closed and queue.empty()``.

Pre-fix, dequeuing the sentinel returned None with work still queued; the
receiver abandoned a method already frozen to it, and drain_for_handoff's
re-route then double-bound the frozen method (DoubleAssignmentError, the
hamilton_smc premature-close crash).
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware_state import LabwareSlot
from orca.workflow_models.method import ExecutingMethod


def _queued_method(name: str) -> MagicMock:
    method = MagicMock(spec=ExecutingMethod)
    method.name = name
    # ``completed`` is set in __init__, so spec does not auto-provide it;
    # await_next_method consults ``.completed.is_set()`` to skip served methods.
    method.completed = MagicMock()
    method.completed.is_set.return_value = False
    return method


class TestSentinelIsAWakeupNotAVerdict:
    @pytest.mark.asyncio
    async def test_work_queued_behind_the_sentinel_is_served(self) -> None:
        """close() then enqueue: the method is served, not abandoned."""
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        stop = asyncio.Event()
        slot.close()
        method = _queued_method("m1")
        slot.queue.put_nowait(method)

        assert await slot.await_next_method(stop) is method
        assert await slot.await_next_method(stop) is None

    @pytest.mark.asyncio
    async def test_clean_close_returns_none(self) -> None:
        """close() on an empty queue: closed and drained means None."""
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        slot.close()

        assert await slot.await_next_method(asyncio.Event()) is None

    @pytest.mark.asyncio
    async def test_stop_event_wins_over_queued_method(self) -> None:
        """A set stop_event returns None even with a method waiting.

        The dequeued-in-race method is dropped, not re-queued (pre-existing:
        stop terminals never drain, so the method is orphaned either way).
        """
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        stop = asyncio.Event()
        stop.set()
        slot.queue.put_nowait(_queued_method("m1"))

        assert await slot.await_next_method(stop) is None

    @pytest.mark.asyncio
    async def test_completed_method_behind_sentinel_is_skipped_live_one_served(
            self) -> None:
        """The skip CONTINUES past a served method to real work behind it.

        Asserting only None could not distinguish the skip path from the old
        sentinel short-circuit; serving the live method proves both the skip
        and the continue, and goes red on the pre-fix engine.
        """
        slot = LabwareSlot(slot_key="tips", labware_template_name="tips")
        slot.close()
        done = _queued_method("served")
        done.completed.is_set.return_value = True
        live = _queued_method("live")
        slot.queue.put_nowait(done)
        slot.queue.put_nowait(live)

        assert await slot.await_next_method(asyncio.Event()) is live
        assert await slot.await_next_method(asyncio.Event()) is None
