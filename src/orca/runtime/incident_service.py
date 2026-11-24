"""IncidentService: DB-agnostic orchestration over an ``IIncidentStore``.

``record`` and ``acknowledge`` are synchronous and enqueue onto a ``SaveQueue``,
because driver callbacks fire them off the event loop and cannot await.
Persistence happens on the loop in two race-free ways that share the queue's
lock so a queued ack never lands before its insert:

* an event-driven background drain (woken thread-safely from off-loop writers)
  persists queued mutations promptly, even for incidents nobody reads; and
* reads (``get``/``list``/``acknowledge_all``) flush the queue first, giving
  read-your-writes consistency without waiting on the drain.

``record`` emits at record-time via the ``on_record`` seam so operators see
faults without waiting on persistence -- safe because every event sink hands the
event to a thread-safe queue or ``call_soon_threadsafe`` rather than doing
loop-bound work inline.

An insert the store refuses is tried again, then kept in memory and handed to
the unsaved-record reporter, and the drain goes on to the next mutation. Reads
return the kept incidents with the stored ones, so an incident the database
could not take is still listed. A read the store fails returns the kept ones
alone when there are any.

On-loop-only entities (deck, teachpoint, ...) write awaited-directly and need
none of this queue/drain/flush machinery.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

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
from orca.runtime.save_queue import SaveQueue
from orca.runtime.unsaved_record import RETRY_DELAYS_SECONDS, IUnsavedRecordReporter, UnsavedRecord

logger = logging.getLogger(__name__)

UNSAVED_KEPT = 1_000
"""How many incidents the store refused are kept in memory. The oldest goes first."""


@dataclasses.dataclass(frozen=True)
class _InsertMutation:
    incident: SystemIncident


@dataclasses.dataclass(frozen=True)
class _AcknowledgeMutation:
    incident_id: str
    acknowledged_at: datetime


_Mutation = _InsertMutation | _AcknowledgeMutation


class IIncidentService(Protocol):
    """Incidents: writes that are safe off the loop, and reads that see every earlier write."""

    on_record: Callable[[SystemIncident], None] | None

    def record(
        self,
        category: IncidentCategory,
        severity: IncidentSeverity,
        message: str,
        detail: IncidentDetail,
        recovery_action: RecoveryAction,
        execution_id: str | None = None,
        thread_id: str | None = None,
    ) -> SystemIncident: ...

    def acknowledge(self, incident_id: str) -> None: ...

    async def get(self, incident_id: str) -> SystemIncident: ...

    async def list(
        self,
        *,
        unacknowledged_only: bool = False,
        category: IncidentCategory | None = None,
        execution_id: str | None = None,
        since: float | None = None,
    ) -> list[SystemIncident]: ...

    async def acknowledge_all(self, *, category: IncidentCategory | None = None) -> int: ...

    async def flush_pending(self) -> None: ...

    def report_unsaved_to(self, reporter: IUnsavedRecordReporter) -> None: ...


class IncidentService:
    """Orchestrates incident recording and queries over a per-DB store."""

    def __init__(self, store: IIncidentStore, retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS) -> None:
        self._store = store
        self._queue: SaveQueue[_Mutation] = SaveQueue(self._apply, self._give_up, retry_delays)
        self._unsaved: dict[str, SystemIncident] = {}
        self._unsaved_records: IUnsavedRecordReporter | None = None
        # Set by the runtime to emit an INCIDENT event at record-time.
        self.on_record: Callable[[SystemIncident], None] | None = None

    def report_unsaved_to(self, reporter: IUnsavedRecordReporter) -> None:
        """Hand each mutation the store refuses to `reporter`. Until then it is logged at error level only."""
        self._unsaved_records = reporter

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
        self._queue.put(_InsertMutation(incident=incident))
        if self.on_record is not None:
            self.on_record(incident)
        return incident

    def acknowledge(self, incident_id: str) -> None:
        """Mark one incident acknowledged. Stamps the ack time when the operator
        acts (not when the drain runs) and enqueues. Idempotent at the store."""
        self._queue.put(
            _AcknowledgeMutation(incident_id=incident_id, acknowledged_at=utc_now())
        )

    async def get(self, incident_id: str) -> SystemIncident:
        async with self._queue.flushed():
            kept = self._unsaved.get(incident_id)
            if kept is not None:
                return kept
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
        async with self._queue.flushed():
            kept = [
                incident for incident in self._unsaved.values()
                if not (unacknowledged_only and incident.acknowledged)
                and (category is None or incident.category is category)
                and (execution_id is None or incident.execution_id == execution_id)
                and (since is None or incident.timestamp >= since)
            ]
            try:
                stored = await self._store.fetch(
                    unacknowledged_only=unacknowledged_only,
                    category=category,
                    execution_id=execution_id,
                    since=since,
                )
            except Exception:
                if not self._unsaved:
                    raise
                logger.warning(
                    "the incident store could not be read; listing the unsaved incidents only", exc_info=True,
                )
                stored = []
        if not kept:
            return stored
        return sorted([*stored, *kept], key=lambda incident: incident.timestamp)

    async def acknowledge_all(
        self, *, category: IncidentCategory | None = None
    ) -> int:
        async with self._queue.flushed():
            kept = [
                incident for incident in self._unsaved.values()
                if not incident.acknowledged and (category is None or incident.category is category)
            ]
            for incident in kept:
                self._unsaved[incident.id] = dataclasses.replace(incident, acknowledged=True)
            return len(kept) + await self._store.mark_all_acked(category, utc_now())

    async def ensure_schema(self) -> None:
        """Create the store's schema if absent (in-memory/sim setup)."""
        await self._store.create_schema()

    @property
    def has_pending(self) -> bool:
        return self._queue.has_pending

    @property
    def is_draining(self) -> bool:
        return self._queue.is_draining

    async def flush_pending(self) -> None:
        """Apply every queued mutation, in FIFO order, before returning."""
        await self._queue.flush()

    async def _apply(self, mutation: _Mutation) -> None:
        if isinstance(mutation, _InsertMutation):
            await self._store.insert(mutation.incident)
            return
        kept = self._unsaved.get(mutation.incident_id)
        if kept is not None:
            self._unsaved[kept.id] = dataclasses.replace(kept, acknowledged=True)
            return
        await self._store.mark_acked(mutation.incident_id, mutation.acknowledged_at)

    def _give_up(self, mutation: _Mutation, error: Exception) -> None:
        if isinstance(mutation, _InsertMutation):
            incident = mutation.incident
            self._keep(incident)
            unsaved = UnsavedRecord(
                kind="an incident",
                record=repr(incident),
                execution_id=incident.execution_id,
                reports_an_unsaved_record=incident.category is IncidentCategory.RECORD_NOT_SAVED,
            )
        else:
            unsaved = UnsavedRecord(
                kind="the acknowledgement of an incident", record=repr(mutation), execution_id=None,
            )
        if self._unsaved_records is None:
            logger.error("could not save %s: %s", unsaved.kind, unsaved.record, exc_info=error)
            return
        self._unsaved_records.report(unsaved, error)

    def _keep(self, incident: SystemIncident) -> None:
        self._unsaved[incident.id] = incident
        while len(self._unsaved) > UNSAVED_KEPT:
            del self._unsaved[next(iter(self._unsaved))]

    def start_drain_task(self) -> asyncio.Task[None]:
        """Start the event-driven background drain. Call once per process (in start)."""
        return self._queue.start()

    async def stop_drain_task(self) -> None:
        """Stop the drain and persist everything still queued."""
        await self._queue.stop()

    async def aclose(self) -> None:
        """Stop the drain, persist what is queued, and release the store
        (dispose its engine). Call from the owner's shutdown."""
        await self.stop_drain_task()
        await self._store.aclose()
