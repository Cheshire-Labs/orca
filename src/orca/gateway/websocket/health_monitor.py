"""Periodic stale-connection sweep for device-bridge WebSocket connections.

Owns the sweep task lifecycle (start / stop / tick cadence) and the
threshold the dispatcher uses to decide a connection is dead. The actual
prune of dead connections stays on :class:`ConnectionManager` because the
connection map and its lock live there -- this class just schedules the
call and forwards the dropped pairs to the on-dropped callback.

Split from :class:`ConnectionManager` so wire-health concerns
(heartbeat-driven liveness) are testable in isolation from connection
state (connect / disconnect / send) and so the broader timeouts redesign
has a single home for "is the wire alive?" decisions.
"""

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional, Tuple

from orca.gateway.websocket.manager import ConnectionManager

logger = logging.getLogger(__name__)


WireHealthCallback = Callable[
    [List[Tuple[str, List[str]]]], Awaitable[None],
]


class WireHealthMonitor:
    """Periodic stale-connection sweep over a :class:`ConnectionManager`.

    Each tick calls :meth:`ConnectionManager.cleanup_stale_connections`
    with the configured threshold and forwards the dropped client/device
    pairs to ``on_dropped`` so callers can emit device-disconnected events.

    Lifecycle is idempotent on both start and stop -- a second call is a
    no-op. The sweep runs on the loop that called ``start``.
    """

    def __init__(
        self,
        manager: ConnectionManager,
        *,
        interval_seconds: float = 30.0,
        timeout_seconds: int = 90,
    ) -> None:
        self._manager = manager
        self._interval_seconds = interval_seconds
        self._timeout_seconds = timeout_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._sweep_count = 0

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    @property
    def sweep_count(self) -> int:
        """Monotonic count of completed sweep ticks (liveness/observability)."""
        return self._sweep_count

    @property
    def is_running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    def start(self, on_dropped: WireHealthCallback) -> None:
        """Schedule the periodic sweep. Idempotent."""
        if self.is_running:
            logger.debug("WireHealthMonitor already running; skipping start")
            return

        stop_event = asyncio.Event()
        self._stop_event = stop_event

        async def _run() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._interval_seconds,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    dropped = await self._manager.cleanup_stale_connections(
                        timeout_seconds=self._timeout_seconds,
                    )
                    if dropped:
                        await on_dropped(dropped)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Wire health sweep tick failed")
                self._sweep_count += 1

        self._task = asyncio.create_task(_run())
        logger.info(
            "Wire health monitor started (interval=%ss, timeout=%ss)",
            self._interval_seconds, self._timeout_seconds,
        )

    async def stop(self) -> None:
        """Stop the sweep. Idempotent; waits up to 5s for clean shutdown."""
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                except (asyncio.CancelledError, Exception):
                    # Task ended in cancellation or error; swallow so stop
                    # stays idempotent and lifespan shutdown can continue.
                    pass
        finally:
            self._task = None
            self._stop_event = None
