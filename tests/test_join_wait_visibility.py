"""The join wait is visible: an empty-handed entry fires the wait callback.

``_BoundSlotView.await_next_method`` fires ``on_empty_wait`` before
delegating, gated exactly like the slot's awaiting-join record: only when the
queue is empty (a receiver with work in hand is one turn from executing, not
waiting) and the slot is not already closed-and-drained (that entry returns
immediately; publishing a wait would be a false status). The thread wires the
callback to fire ``ThreadEvent.CO_LABWARE_AWAITED``, making a receiver
suspended at a join visible to operators and the stall detector; the
full-runtime pin lives in ``test_closure_crash_fail_open.py``, and the
STOPPING-race and post-dequeue-move table entries in
``test_thread_state_machine.py``.
"""
import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware_state import IRegisteredThread, LabwareSlot
from orca.workflow_models.labware_threads.executing_labware_thread import (
    _BoundSlotView,
)
from orca.workflow_models.method import ExecutingMethod


def _waiter() -> IRegisteredThread:
    stub = SimpleNamespace(
        thread_instance=SimpleNamespace(
            thread_template=None, group_id=None, submission_id="s1",
        ),
        has_completed=lambda: False,
        has_finished_its_work=lambda: False,
    )
    return cast(IRegisteredThread, stub)


def _queued_method() -> MagicMock:
    method = MagicMock(spec=ExecutingMethod)
    # ``completed`` is set in __init__, so spec does not auto-provide it.
    method.completed = MagicMock()
    method.completed.is_set.return_value = False
    return method


def _stopped() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@pytest.mark.asyncio
async def test_fires_once_per_empty_handed_entry() -> None:
    slot = LabwareSlot("pool:*:s1", "pool")
    fired: list[int] = []
    view = _BoundSlotView(slot, _waiter(), on_empty_wait=lambda: fired.append(1))

    assert await view.await_next_method(_stopped()) is None
    assert len(fired) == 1
    assert await view.await_next_method(_stopped()) is None
    assert len(fired) == 2


@pytest.mark.asyncio
async def test_no_fire_with_queued_work() -> None:
    slot = LabwareSlot("pool:*:s1", "pool")
    method = _queued_method()
    slot.queue.put_nowait(method)
    fired: list[int] = []
    view = _BoundSlotView(slot, _waiter(), on_empty_wait=lambda: fired.append(1))

    assert await view.await_next_method(asyncio.Event()) is method
    assert fired == []


@pytest.mark.asyncio
async def test_no_fire_when_closed_and_drained() -> None:
    slot = LabwareSlot("pool:*:s1", "pool")
    slot.close()
    # Consume the wakeup sentinel so the slot is closed AND drained.
    slot.queue.get_nowait()
    fired: list[int] = []
    view = _BoundSlotView(slot, _waiter(), on_empty_wait=lambda: fired.append(1))

    assert await view.await_next_method(asyncio.Event()) is None
    assert fired == []


@pytest.mark.asyncio
async def test_no_fire_with_only_the_close_sentinel_queued() -> None:
    """A closed slot holding only the wakeup sentinel exits without serving;
    the entry is not a wait and must not be published as one."""
    slot = LabwareSlot("pool:*:s1", "pool")
    slot.close()
    fired: list[int] = []
    view = _BoundSlotView(slot, _waiter(), on_empty_wait=lambda: fired.append(1))

    assert await view.await_next_method(asyncio.Event()) is None
    assert fired == []


@pytest.mark.asyncio
async def test_fire_precedes_the_wait() -> None:
    """Work enqueued BY the callback is observed by the wait it precedes;
    a post-wait fire would leave this call blocked forever."""
    slot = LabwareSlot("pool:*:s1", "pool")
    method = _queued_method()
    view = _BoundSlotView(
        slot, _waiter(), on_empty_wait=lambda: slot.queue.put_nowait(method),
    )

    result = await asyncio.wait_for(
        view.await_next_method(asyncio.Event()), timeout=5.0,
    )
    assert result is method


@pytest.mark.asyncio
async def test_no_callback_wired_is_silent() -> None:
    slot = LabwareSlot("pool:*:s1", "pool")
    view = _BoundSlotView(slot, _waiter())

    assert await view.await_next_method(_stopped()) is None
