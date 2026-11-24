"""Periodic stale-connection sweep for device-bridge WebSocket connections.

Owns the sweep task lifecycle (start / stop / tick cadence) and the
threshold that decides a connection is dead. Closing a dead connection is
:class:`ConnectionManager`'s; the router's cleanup for that socket unregisters
the client and reports its devices disconnected.

Split from :class:`ConnectionManager` so wire-health concerns
(heartbeat-driven liveness) are testable in isolation from connection
state (connect / disconnect / send) and so the broader timeouts redesign
has a single home for "is the wire alive?" decisions.
"""

import asyncio
import logging
from typing import Optional

from orca.gateway.registry.connection_tracker import HEARTBEAT_TOLERANCE_SECONDS
from orca.gateway.websocket.manager import IConnectionManager

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS: float = 5.0


class WireHealthMonitor:
    """Periodic stale-connection sweep over a :class:`ConnectionManager`.

    Each tick calls :meth:`ConnectionManager.close_stale_connections`
    with the configured threshold. By default a socket is closed one interval before
    `HEARTBEAT_TOLERANCE_SECONDS`, so it is closed by the time the registry calls its devices offline.

    Lifecycle is idempotent on both start and stop -- a second call is a
    no-op. The sweep runs on the loop that called ``start``.
    """

    def __init__(
        self,
        manager: IConnectionManager,
        *,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._manager = manager
        self._interval_seconds = interval_seconds
        self._timeout_seconds = HEARTBEAT_TOLERANCE_SECONDS - interval_seconds if timeout_seconds is None else timeout_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._sweep_count = 0

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def sweep_count(self) -> int:
        """Monotonic count of completed sweep ticks (liveness/observability)."""
        return self._sweep_count

    @property
    def is_running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    def start(self) -> None:
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
                    await self._manager.close_stale_connections(timeout_seconds=self._timeout_seconds)
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
