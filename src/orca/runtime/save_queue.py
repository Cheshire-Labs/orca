"""A thread-safe queue of mutations, applied in order on the event loop."""

import asyncio
import logging
import queue
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Generic, TypeVar

from orca.runtime.unsaved_record import RETRY_DELAYS_SECONDS

logger = logging.getLogger(__name__)

Mutation = TypeVar("Mutation")


class SaveQueue(Generic[Mutation]):
    """Queued mutations are applied by a background drain and by ``flush``, under one lock.

    A mutation ``apply`` raises on is tried again after each of ``retry_delays``, then
    handed to ``give_up`` and left out, so the mutations after it are still applied.
    After one is given up on, the rest of that pass get one try each, so an outage
    does not hold a read behind a backoff per mutation.
    """

    def __init__(
        self,
        apply: Callable[[Mutation], Awaitable[None]],
        give_up: Callable[[Mutation, Exception], None],
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
    ) -> None:
        self._apply = apply
        self._give_up = give_up
        self._retry_delays = tuple(retry_delays)
        self._pending: queue.Queue[Mutation] = queue.Queue()
        self._lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._stop = False

    @property
    def has_pending(self) -> bool:
        return not self._pending.empty()

    @property
    def is_draining(self) -> bool:
        return self._drain_task is not None and not self._drain_task.done()

    def put(self, mutation: Mutation) -> None:
        """Enqueue and wake the drain. Safe from any thread."""
        self._pending.put(mutation)
        self._signal()

    def _signal(self) -> None:
        """Wake the on-loop drain thread-safely; no-op if no drain is running."""
        loop, wakeup = self._loop, self._wakeup
        if loop is None or wakeup is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wakeup.set)
        except RuntimeError:
            # The loop closed between the check and the call, and a synchronous caller
            # must not raise over a drain that is gone. Any other fault still surfaces.
            if not loop.is_closed():
                raise

    async def flush(self) -> None:
        """Apply everything queued, in order. It waits out a drain in flight, so a read after it sees every earlier write."""
        async with self._lock:
            await self._drain_available()

    @asynccontextmanager
    async def flushed(self) -> AsyncIterator[None]:
        """Everything queued is applied, and nothing more is applied until the block ends.

        For a read: on a database with one connection, a read that closes beside a save rolls the save back.
        """
        async with self._lock:
            await self._drain_available()
            yield

    async def _drain_available(self) -> None:
        """Apply queued mutations until the queue empties. Caller holds the lock."""
        delays = self._retry_delays
        while True:
            try:
                mutation = self._pending.get_nowait()
            except queue.Empty:
                return
            if not await self._applied(mutation, delays):
                delays = ()

    async def _applied(self, mutation: Mutation, delays: tuple[float, ...]) -> bool:
        for delay in (*delays, None):
            try:
                await self._apply(mutation)
                return True
            except Exception as error:
                if delay is None:
                    self._gave_up(mutation, error)
                    return False
                await asyncio.sleep(delay)
        return False

    def _gave_up(self, mutation: Mutation, error: Exception) -> None:
        try:
            self._give_up(mutation, error)
        except Exception:
            logger.exception("could not report the unsaved %r", mutation)

    def start(self) -> asyncio.Task[None]:
        """Start the background drain, or return the one already running."""
        if self._drain_task is not None and not self._drain_task.done():
            return self._drain_task
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._stop = False
        self._drain_task = asyncio.create_task(self._drain_loop())
        return self._drain_task

    async def stop(self) -> None:
        """Stop the drain and persist everything still queued."""
        self._stop = True
        if self._wakeup is not None:
            self._wakeup.set()
        if self._drain_task is not None:
            await self._drain_task
            self._drain_task = None
        # Drain to empty: an off-loop straggler (a driver callback racing
        # teardown) may enqueue as we stop, so loop until nothing remains.
        while not self._pending.empty():
            await self.flush()
        self._loop = None
        self._wakeup = None

    async def _drain_loop(self) -> None:
        assert self._wakeup is not None
        while not self._stop:
            await self._wakeup.wait()
            self._wakeup.clear()
            if self._stop:
                return
            async with self._lock:
                await self._drain_available()
