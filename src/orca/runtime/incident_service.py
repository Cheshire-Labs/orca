"""IncidentService: DB-agnostic orchestration over an ``IIncidentStore``.

``record`` and ``acknowledge`` are synchronous and enqueue onto a thread-safe
queue, because driver callbacks fire them off the event loop and cannot await.
Persistence happens on the loop in two race-free ways that share one
``_apply_lock`` so a queued ack never lands before its insert:

* an event-driven background drain (woken thread-safely from off-loop writers)
  persists queued mutations promptly, even for incidents nobody reads; and
* reads (``get``/``list``/``acknowledge_all``) flush the queue first, giving
  read-your-writes consistency without waiting on the drain.

``record`` emits at record-time via the ``on_record`` seam so operators see
faults without waiting on persistence -- safe because every event sink hands the
event to a thread-safe queue or ``call_soon_threadsafe`` rather than doing
loop-bound work inline.

This is the reference shape for the persistence verticals' off-loop variant.
On-loop-only entities (deck, teachpoint, ...) write awaited-directly and need
none of this queue/drain/flush machinery.
"""

import asyncio
import dataclasses
import queue
from datetime import datetime
from typing import Callable

from orca.runtime.db.base import utc_now
from orca.runtime.incident_store import (
    IIncidentStore,
    IncidentCategory,
    IncidentDetail,
    IncidentSeverity,
    RecoveryAction,
    SystemIncident,
    build_incident,
)


@dataclasses.dataclass(frozen=True)
class _InsertMutation:
    incident: SystemIncident


@dataclasses.dataclass(frozen=True)
class _AcknowledgeMutation:
    incident_id: str
    acknowledged_at: datetime


_Mutation = _InsertMutation | _AcknowledgeMutation


class IncidentService:
    """Orchestrates incident recording and queries over a per-DB store."""

    def __init__(self, store: IIncidentStore) -> None:
        self._store = store
        self._pending: queue.Queue[_Mutation] = queue.Queue()
        self._apply_lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._stop = False
        # Set by the runtime to emit an INCIDENT event at record-time.
        self.on_record: Callable[[SystemIncident], None] | None = None

    def record(
        self,
        category: IncidentCategory,
        severity: IncidentSeverity,
        message: str,
        detail: IncidentDetail,
        recovery_action: RecoveryAction,
        execution_id: str | None = None,
        thread_id: str | None = None,
    ) -> SystemIncident:
        """Record a new incident. Sync + off-loop safe: builds the incident,
        enqueues the insert, wakes the drain, emits at record-time, returns it."""
        incident = build_incident(
            category, severity, message, detail, recovery_action,
            execution_id, thread_id,
        )
        self._pending.put(_InsertMutation(incident=incident))
        self._signal()
        if self.on_record is not None:
            self.on_record(incident)
        return incident

    def acknowledge(self, incident_id: str) -> None:
        """Mark one incident acknowledged. Stamps the ack time when the operator
        acts (not when the drain runs) and enqueues. Idempotent at the store."""
        self._pending.put(
            _AcknowledgeMutation(incident_id=incident_id, acknowledged_at=utc_now())
        )
        self._signal()

    def _signal(self) -> None:
        """Wake the on-loop drain thread-safely; no-op if no drain is running."""
        loop, wakeup = self._loop, self._wakeup
        if loop is None or wakeup is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wakeup.set)
        except RuntimeError:
            # The loop closed between the check and the call. The caller is
            # synchronous and cannot be allowed to raise over a drain that is
            # already gone. Anything else is a real fault and still surfaces.
            if not loop.is_closed():
                raise

    async def get(self, incident_id: str) -> SystemIncident:
        await self.flush_pending()
        incident = await self._store.get(incident_id)
        if incident is None:
            raise KeyError(f"No incident with id '{incident_id}'")
        return incident

    async def list(
        self,
        *,
        unacknowledged_only: bool = False,
        category: IncidentCategory | None = None,
        execution_id: str | None = None,
        since: float | None = None,
    ) -> list[SystemIncident]:
        await self.flush_pending()
        return await self._store.fetch(
            unacknowledged_only=unacknowledged_only,
            category=category,
            execution_id=execution_id,
            since=since,
        )

    async def acknowledge_all(
        self, *, category: IncidentCategory | None = None
    ) -> int:
        await self.flush_pending()
        return await self._store.mark_all_acked(category, utc_now())

    async def ensure_schema(self) -> None:
        """Create the store's schema if absent (in-memory/sim setup)."""
        await self._store.create_schema()

    async def flush_pending(self) -> None:
        """Apply every currently-queued mutation, in FIFO order, before returning.

        Taking the lock here is what gives reads their read-your-writes: a read
        that flushes waits out any in-flight drain, so a just-recorded incident
        is committed before the caller queries the store."""
        async with self._apply_lock:
            await self._drain_available()

    async def _drain_available(self) -> None:
        """Apply queued mutations until the queue empties. Caller holds the lock."""
        while True:
            try:
                mutation = self._pending.get_nowait()
            except queue.Empty:
                return
            await self._apply(mutation)

    async def _apply(self, mutation: _Mutation) -> None:
        if isinstance(mutation, _InsertMutation):
            await self._store.insert(mutation.incident)
        else:
            await self._store.mark_acked(
                mutation.incident_id, mutation.acknowledged_at
            )

    def start_drain_task(self) -> asyncio.Task[None]:
        """Start the event-driven background drain. Call once per process (in start)."""
        if self._drain_task is not None and not self._drain_task.done():
            return self._drain_task
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._stop = False
        self._drain_task = asyncio.create_task(self._drain_loop())
        return self._drain_task

    async def stop_drain_task(self) -> None:
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
            await self.flush_pending()
        self._loop = None
        self._wakeup = None

    async def aclose(self) -> None:
        """Stop the drain, persist what is queued, and release the store
        (dispose its engine). Call from the owner's shutdown."""
        await self.stop_drain_task()
        await self._store.aclose()

    async def _drain_loop(self) -> None:
        assert self._wakeup is not None
        while not self._stop:
            await self._wakeup.wait()
            self._wakeup.clear()
            if self._stop:
                return
            async with self._apply_lock:
                await self._drain_available()
