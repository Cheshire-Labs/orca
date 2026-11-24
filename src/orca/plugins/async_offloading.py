"""AsyncOffloadingPlugin: an OrcaPlugin that keeps I/O off the emit path.

SystemEventBus.emit is synchronous. A plugin doing network I/O
directly in handle_runtime_event would block the runtime for the duration of
every outbound call. This base enqueues each event and drains it on a
background task: a thread-safe stdlib ``queue.Queue`` plus a drain loop woken
by an ``asyncio.Event``. (``queue.Queue`` rather than ``asyncio.Queue`` so the
handoff is correct no matter which thread calls the synchronous ``emit``;
the wake crosses back to the loop with ``call_soon_threadsafe``.) An idle
drain holds no worker thread, so nothing survives its own cancellation.
Under sustained backpressure the oldest queued event is dropped so a slow
or erroring integration can never stall emit. Shutdown is bounded: ``stop``
abandons the backlog rather than draining it into a failing endpoint.
"""

import asyncio
import logging
import queue

from orca.events.execution_context import HostedEventContext
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin

logger = logging.getLogger(__name__)


class AsyncOffloadingPlugin(OrcaPlugin):
    """OrcaPlugin base that delivers events on a background drain task.

    Subclasses implement ``deliver`` with the actual outbound call. The
    drain task is started via ``start()`` (idempotent) once a loop is
    running and stopped via ``stop()``.
    """

    def __init__(
        self,
        queue_size: int,
        stop_drain_timeout: float,
    ) -> None:
        self._queue: queue.Queue[RuntimeEvent] = queue.Queue(queue_size)
        self._drain_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._stopping = False
        self._stop_drain_timeout = stop_drain_timeout
        self._dropped_count = 0

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        self._enqueue(event)
        self._wake()

    def on_event(self, event: RuntimeEvent) -> None:
        """Take an event as a sink of a server's event hub. A hosted server's own events are its to send."""
        if isinstance(event.context, HostedEventContext):
            return
        self.handle_runtime_event(event)

    def start(self) -> asyncio.Task[None]:
        """Start the background drain task. Idempotent across rebuilds."""
        if self._drain_task is not None and not self._drain_task.done():
            return self._drain_task
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._drain_task = asyncio.create_task(self._drain_loop())
        return self._drain_task

    async def stop(self) -> None:
        """Stop the drain, abandoning the backlog.

        A full drain would re-introduce at shutdown the exact stall the
        offload exists to avoid: a down endpoint x a full queue x the retry
        ladder is minutes. So set the stop flag (the drain returns on its
        next wake), wake it, and cap the wait -- cancelling aborts an
        in-flight delivery.
        """
        self._stopping = True
        if self._drain_task is None:
            return
        self._wake()
        try:
            await asyncio.wait_for(self._drain_task, timeout=self._stop_drain_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "%s drain exceeded %.1fs at shutdown; backlog abandoned",
                type(self).__name__, self._stop_drain_timeout,
            )
        except asyncio.CancelledError:
            pass
        finally:
            self._drain_task = None
            self._loop = None
            self._wakeup = None

    @property
    def dropped_count(self) -> int:
        """Events evicted under backpressure since construction."""
        return self._dropped_count

    async def deliver(self, event: RuntimeEvent) -> None:
        """Push one event to the external system. Override in subclasses."""
        raise NotImplementedError

    def _enqueue(self, event: RuntimeEvent) -> None:
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
            self._dropped_count += 1
            logger.warning(
                "%s queue full; dropped oldest event (%d dropped total)",
                type(self).__name__, self._dropped_count,
            )
        except queue.Empty:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning(
                "%s queue still full after eviction; dropping new event",
                type(self).__name__,
            )

    def _wake(self) -> None:
        """Wake the on-loop drain thread-safely; no-op if no drain is running.

        ``emit`` is synchronous and arrives on whatever thread the runtime
        dispatched on, so the wake has to cross back to the drain's loop.
        """
        loop, wakeup = self._loop, self._wakeup
        if loop is None or wakeup is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wakeup.set)
        except RuntimeError:
            # The loop closed between the check and the call. The producer is sync and
            # must not raise over a dead drain; anything else still surfaces.
            if not loop.is_closed():
                raise

    async def _drain_loop(self) -> None:
        assert self._wakeup is not None
        while not self._stopping:
            await self._wakeup.wait()
            self._wakeup.clear()
            while not self._stopping:
                try:
                    event = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    await self.deliver(event)
                except Exception as exc:
                    logger.warning(
                        "%s delivery failed: %s", type(self).__name__, exc,
                    )
