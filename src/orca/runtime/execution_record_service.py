"""ExecutionRecordService: DB-agnostic orchestration over an ``IExecutionRecordStore``.

The runtime writes run status where each transition happens: a submission
accepted, a thread changing status, a run ending. Those callers are
synchronous (a thread's status change, a task done-callback), so
``record_running``, ``record_thread`` and ``mark_terminal`` enqueue onto a
``SaveQueue``. Persistence happens on the loop in two race-free ways that
share the queue's lock so a queued terminal never lands before its insert:

* an event-driven background drain (woken thread-safely from off-loop writers)
  persists queued mutations promptly; and
* reads flush the queue first, giving read-your-writes consistency.

A row the store refuses is tried again, then handed to the unsaved-record
reporter, which pauses the row's run and raises an incident. The rows after it
are still saved.

The persisted status IS an ``ExecutionState``, so mapping a row to the runtime
``ExecutionRecord`` / ``ExecutionDetail`` is a direct field copy.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Sequence
from typing import Protocol
from datetime import datetime

from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.execution_record_store import (
    IExecutionRecordStore,
    PersistedExecution,
    PersistedThread,
)
from orca.runtime.save_queue import SaveQueue
from orca.runtime.status_models import ExecutionDetail, ThreadSnapshot
from orca.runtime.unsaved_record import RETRY_DELAYS_SECONDS, IUnsavedRecordReporter, UnsavedRecord

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _PendingInsert:
    execution_id: str
    workflow_name: str
    submitted_at: datetime


@dataclasses.dataclass(frozen=True)
class _PendingTerminal:
    execution_id: str
    state: ExecutionState
    reason: str | None
    terminal_at: datetime


@dataclasses.dataclass(frozen=True)
class _PendingThread:
    execution_id: str
    thread: PersistedThread


_Mutation = _PendingInsert | _PendingTerminal | _PendingThread


def _to_record(p: PersistedExecution) -> ExecutionRecord:
    return ExecutionRecord(
        id=p.execution_id,
        workflow_name=p.workflow_name,
        status=p.status,
        error=p.terminal_reason,
    )


def _to_thread_snapshot(t: PersistedThread) -> ThreadSnapshot:
    """Persisted summary -> snapshot. Location and method progress are not
    persisted (the ops history holds the action trail); labware identity
    mirrors the thread's, which holds by construction for labware threads."""
    return ThreadSnapshot(
        id=t.thread_id,
        name=t.name,
        status=t.status,
        current_location=None,
        current_method=None,
        completed_method_count=0,
        last_error=t.last_error,
        pause_reason=t.pause_reason,
        completed_methods=(),
        labware_id=t.thread_id,
        labware_name=t.name,
        group_id=t.group_id,
    )


def _to_detail(p: PersistedExecution, threads: list[PersistedThread]) -> ExecutionDetail:
    snapshots = [_to_thread_snapshot(t) for t in threads]
    completed = sum(1 for t in snapshots if t.status == "COMPLETED")
    # Same terminal set as the live builder in system_runtime.get_execution_detail.
    active = sum(
        1 for t in snapshots
        if t.status not in ("COMPLETED", "ABORTED", "STOPPED", "FAILED", "STOPPING")
    )
    return ExecutionDetail(
        id=p.execution_id,
        workflow_name=p.workflow_name,
        status=p.status.value,
        error=p.terminal_reason,
        threads=snapshots,
        total_thread_count=len(snapshots),
        completed_thread_count=completed,
        active_thread_count=active,
    )


class IExecutionRecordService(Protocol):
    """Run records: writes that are safe off the loop, and reads that see every earlier write."""

    def record_running(self, execution_id: str, workflow_name: str, submitted_at: datetime) -> None: ...

    def mark_terminal(
        self, execution_id: str, state: ExecutionState, reason: str | None, terminal_at: datetime,
    ) -> None: ...

    def record_thread(
        self, execution_id: str, *, thread_id: str, name: str,
        template_name: str, status: str,
        last_error: str | None = None, pause_reason: str | None = None,
        group_id: str | None = None,
    ) -> None: ...

    async def get_record(self, execution_id: str) -> ExecutionRecord | None: ...

    async def list_records(self) -> list[ExecutionRecord]: ...

    async def get_detail(self, execution_id: str) -> ExecutionDetail | None: ...

    async def contains(self, execution_id: str) -> bool: ...

    async def list_non_terminal(self) -> list[PersistedExecution]: ...

    async def mark_interrupted(self, execution_id: str, reason: str, terminal_at: datetime) -> None: ...

    async def delete(self, execution_id: str) -> None: ...

    async def flush_pending(self) -> None: ...


class ExecutionRecordService:
    """Orchestrates execution-record writes and queries over a per-DB store."""

    def __init__(
        self,
        store: IExecutionRecordStore,
        unsaved_records: IUnsavedRecordReporter,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
    ) -> None:
        self._store = store
        self._unsaved_records = unsaved_records
        self._queue: SaveQueue[_Mutation] = SaveQueue(self._apply, self._give_up, retry_delays)

    def record_running(
        self, execution_id: str, workflow_name: str, submitted_at: datetime
    ) -> None:
        """Record a RUNNING insert. Sync + off-loop safe: enqueues + wakes the drain."""
        self._queue.put(
            _PendingInsert(
                execution_id=execution_id,
                workflow_name=workflow_name,
                submitted_at=submitted_at,
            )
        )

    def mark_terminal(
        self, execution_id: str, state: ExecutionState, reason: str | None,
        terminal_at: datetime,
    ) -> None:
        """Record a terminal transition. Sync + off-loop safe: enqueues + wakes."""
        self._queue.put(
            _PendingTerminal(
                execution_id=execution_id,
                state=state,
                reason=reason,
                terminal_at=terminal_at,
            )
        )

    def record_thread(
        self, execution_id: str, *, thread_id: str, name: str,
        template_name: str, status: str,
        last_error: str | None = None, pause_reason: str | None = None,
        group_id: str | None = None,
    ) -> None:
        """Record a thread's latest summary. Sync + off-loop safe: enqueues + wakes."""
        self._queue.put(
            _PendingThread(
                execution_id=execution_id,
                thread=PersistedThread(
                    thread_id=thread_id,
                    name=name,
                    template_name=template_name,
                    status=status,
                    last_error=last_error,
                    pause_reason=pause_reason,
                    group_id=group_id,
                ),
            )
        )

    async def get_record(self, execution_id: str) -> ExecutionRecord | None:
        async with self._queue.flushed():
            persisted = await self._store.get(execution_id)
        return _to_record(persisted) if persisted is not None else None

    async def list_records(self) -> list[ExecutionRecord]:
        async with self._queue.flushed():
            return [_to_record(p) for p in await self._store.list_all()]

    async def get_detail(self, execution_id: str) -> ExecutionDetail | None:
        async with self._queue.flushed():
            persisted = await self._store.get(execution_id)
            if persisted is None:
                return None
            return _to_detail(persisted, await self._store.list_threads(execution_id))

    async def contains(self, execution_id: str) -> bool:
        async with self._queue.flushed():
            return await self._store.get(execution_id) is not None

    async def delete(self, execution_id: str) -> None:
        async with self._queue.flushed():
            await self._store.delete(execution_id)

    async def list_non_terminal(self) -> list[PersistedExecution]:
        async with self._queue.flushed():
            return await self._store.list_non_terminal()

    async def mark_interrupted(
        self, execution_id: str, reason: str, terminal_at: datetime
    ) -> None:
        """Boot-scan transition: a row was non-terminal across a restart. The run
        was lost, so it lands as FAILED with the interrupted reason."""
        async with self._queue.flushed():
            await self._store.mark_terminal(
                execution_id, ExecutionState.FAILED, reason, terminal_at
            )

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

    def _give_up(self, mutation: _Mutation, error: Exception) -> None:
        self._unsaved_records.report(
            UnsavedRecord(kind="the run's status", record=repr(mutation), execution_id=mutation.execution_id),
            error,
        )

    async def _apply(self, mutation: _Mutation) -> None:
        if isinstance(mutation, _PendingInsert):
            await self._store.upsert_running(
                mutation.execution_id, mutation.workflow_name, mutation.submitted_at
            )
        elif isinstance(mutation, _PendingThread):
            await self._store.upsert_thread(mutation.execution_id, mutation.thread)
        else:
            affected = await self._store.mark_terminal(
                mutation.execution_id,
                mutation.state,
                mutation.reason,
                mutation.terminal_at,
            )
            if not affected:
                logger.warning(
                    "execution record: terminal transition for unknown "
                    "execution_id=%s (state=%s); its RUNNING row was never "
                    "saved, or was lost across a process restart",
                    mutation.execution_id, mutation.state.value,
                )

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
