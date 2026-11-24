"""An error while handing out reservations fails that one request and leaves the loop running.

The loop is the only thing that answers every reservation waiter in the system. If it dies, every
move and action waits forever with nothing in the log beyond an unretrieved task exception.
"""
import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import List
from unittest.mock import Mock

import pytest

from orca.system.reservation_manager.errors import UnresolvableDeadlockContext
from orca.system.reservation_manager.interfaces import IReservationCollection
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import ThreadReservationCoordinator
from tests.test_helpers import wait_until


class _Request(IReservationCollection):
    def __init__(self, thread_id: str, fail: bool = False) -> None:
        self._thread_id = thread_id
        self._fail = fail
        self._processed = asyncio.Event()
        self._granted = asyncio.Event()
        self._rejected = asyncio.Event()
        self._deadlocked = asyncio.Event()
        self._unresolvable = asyncio.Event()
        self._context: UnresolvableDeadlockContext | None = None

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def get_reservations(self) -> List[LocationReservation]:
        if self._fail:
            raise RuntimeError("boom")
        return []

    def resolve_final_reservation(self) -> None:
        self._granted.set()
        self._processed.set()

    @property
    def processed(self) -> asyncio.Event:
        return self._processed

    @property
    def granted(self) -> asyncio.Event:
        return self._granted

    @property
    def rejected(self) -> asyncio.Event:
        return self._rejected

    @property
    def deadlocked(self) -> asyncio.Event:
        return self._deadlocked

    @property
    def unresolvable_deadlock(self) -> asyncio.Event:
        return self._unresolvable

    @property
    def unresolvable_deadlock_context(self) -> UnresolvableDeadlockContext | None:
        return self._context

    def set_unresolvable_deadlock(self, context: UnresolvableDeadlockContext) -> None:
        self._context = context
        self._unresolvable.set()

    def clear(self) -> None:
        for event in (self._processed, self._granted, self._rejected, self._deadlocked):
            event.clear()


@pytest.fixture
async def running() -> AsyncIterator[tuple[ThreadReservationCoordinator, "asyncio.Task[None]"]]:
    coordinator = ThreadReservationCoordinator(Mock(), Mock())
    task = asyncio.create_task(coordinator.start_tick_loop())
    await wait_until(lambda: coordinator.ticker_started, timeout=5.0)
    yield coordinator, task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _submit(coordinator: ThreadReservationCoordinator, request: _Request) -> None:
    await coordinator.submit_reservation_request(request.thread_id, request)
    await asyncio.wait_for(request.processed.wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_a_request_that_raises_is_answered_as_not_granted_and_the_next_one_is_served(
    running: tuple[ThreadReservationCoordinator, "asyncio.Task[None]"],
) -> None:
    coordinator, task = running
    broken = _Request("t-broken", fail=True)
    healthy = _Request("t-healthy")

    await _submit(coordinator, broken)
    assert not broken.granted.is_set()
    assert not broken.rejected.is_set()

    await _submit(coordinator, healthy)
    assert healthy.granted.is_set()
    assert not task.done()


@pytest.mark.asyncio
async def test_a_failed_deadlock_pass_does_not_stop_the_loop(
    running: tuple[ThreadReservationCoordinator, "asyncio.Task[None]"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, task = running
    detector = coordinator._deadlock_detector
    real = detector.process_tick_results
    failures = [RuntimeError("detector boom")]

    def fail_once(collections: Sequence[IReservationCollection]) -> None:
        if failures:
            raise failures.pop()
        real(collections)

    monkeypatch.setattr(detector, "process_tick_results", fail_once)

    await _submit(coordinator, _Request("t-1"))
    later = _Request("t-2")
    await _submit(coordinator, later)

    assert later.granted.is_set()
    assert not task.done()
