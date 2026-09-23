"""A device/transporter lock that says who holds it and who is waiting for it.

A plain ``asyncio.Lock`` wait is invisible. A thread queued for a device that
another call is driving keeps its own status -- ``MOVING`` while a transporter
waits to reach into a liquid handler, ``EXECUTING_ACTION`` while a second call
waits its turn -- and reports no wait subject, so an operator looking at a run
that has stopped moving sees a thread in flight and nothing to point at.

Every hold names its purpose, so a waiter can be told what it is behind:
``"flex_1 device lock held by move_plate (plate_1)"``. The waiting thread is
found through ``current_lock_wait``, a ContextVar seeded once per labware
thread. It holds a MUTABLE record rather than the wait string itself, for two
reasons: a device call can run in a child task, where a ContextVar set there
would never reach the thread reading it; and the holder has to be read at the
moment someone looks, not stamped when the wait began, or a queue of three
threads all name a call that finished long ago.

A contended wait logs twice: once when it starts and once with its duration
when it ends. The start line is the one that matters, because a hold that never
returns is the case an operator is reading the log for, and a duration line
cannot describe a wait that has not finished. Both are throttled per lock.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Optional

orca_logger = logging.getLogger("orca")

# Below this a wait is ordinary turn-taking and not worth a line.
_WAIT_WORTH_REPORTING_SECONDS = 1.0
# One line per lock per window: park_gantry retries its acquire every couple
# of seconds for 15 minutes, and unthrottled that buries the rest of the log.
_WAIT_REPORT_INTERVAL_SECONDS = 30.0


class TrackedLock:
    """An exclusive lock that records its holder and publishes its waiters.

    Acquire it only through ``held_for``: a hold with no purpose leaves a
    waiter with nothing to report, which is the state this class exists to end.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = asyncio.Lock()
        self._holder: Optional[str] = None
        self._last_reported = 0.0
        self._last_start_reported = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def holder(self) -> Optional[str]:
        """What the current hold is doing, or None when the lock is free."""
        return self._holder

    def locked(self) -> bool:
        return self._lock.locked()

    @property
    def wait_subject(self) -> str:
        """This lock and whoever holds it right now, in operator words."""
        held_by = self._holder or "a call that has not named itself"
        return f"{self._name} held by {held_by}"

    @asynccontextmanager
    async def held_for(self, purpose: str) -> AsyncIterator[None]:
        """Hold the lock for the duration of the block, on the record."""
        slot = current_lock_wait.get()
        waiter = f"{purpose} ({slot.owner})" if slot is not None else purpose
        started = time.monotonic()
        # No "will this block?" check first: a lock part-way through a release
        # reads unlocked while its queued waiters still have to wait.
        if slot is not None:
            slot.enter_wait(self)
        self._report_wait_started(waiter)
        try:
            await self._lock.acquire()
        finally:
            if slot is not None:
                slot.clear_wait()
        self._report_wait(waiter, time.monotonic() - started)
        self._holder = waiter
        try:
            yield
        finally:
            self._holder = None
            self._lock.release()

    def _report_wait_started(self, waiter: str) -> None:
        """Say the wait began, because a wait that never ends never reports.

        The duration line below only fires once the lock is finally in hand,
        so the worst case, a hold that never returns, is the one case it can
        never describe. That is the case someone is reading the log for.
        """
        if not self._lock.locked():
            return
        now = time.monotonic()
        if now - self._last_start_reported < _WAIT_REPORT_INTERVAL_SECONDS:
            return
        self._last_start_reported = now
        orca_logger.info("%s is waiting for %s", waiter, self.wait_subject)

    def _report_wait(self, waiter: str, waited: float) -> None:
        if waited < _WAIT_WORTH_REPORTING_SECONDS:
            return
        now = time.monotonic()
        if now - self._last_reported < _WAIT_REPORT_INTERVAL_SECONDS:
            return
        self._last_reported = now
        orca_logger.info("%s waited %.1fs for %s", waiter, waited, self._name)


class LockWait:
    """One labware thread's slot: who it is, and what lock it is queued for.

    One slot per thread, so it describes the wait the thread's own task is in.
    A thread whose device call runs in a child task writes the same slot, which
    is the point: the parent is the one anybody reads.
    """

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self._queued_for: Optional[TrackedLock] = None

    @property
    def waiting_on(self) -> Optional[str]:
        """The lock this thread is queued for, named with its CURRENT holder.

        Read live rather than stamped when the wait began: with several threads
        queued, the hold that was in front at the start is usually finished by
        the time anyone looks, and naming it sends the reader after a call that
        already returned.
        """
        queued_for = self._queued_for
        return queued_for.wait_subject if queued_for is not None else None

    def enter_wait(self, lock: TrackedLock) -> None:
        """Start publishing a wait for ``lock``."""
        self._queued_for = lock

    def clear_wait(self) -> None:
        """Stop publishing a wait.

        Cleared rather than restored to whatever was here before. One task
        cannot be queued for two locks at once, so within a thread the slot is
        always already empty; a displaced entry only happens when two tasks
        share the slot, and putting it back there leaves the thread publishing
        a wait nobody is in, for the rest of its life.
        """
        self._queued_for = None


current_lock_wait: ContextVar[Optional[LockWait]] = ContextVar(
    "current_lock_wait", default=None,
)
