"""ExecutionRecordService: DB-agnostic orchestration over an ``IExecutionRecordStore``.

``record_running`` and ``mark_terminal`` are synchronous and enqueue onto a
thread-safe queue, because the event sink fires them off the event loop and
cannot await. Persistence happens on the loop in two race-free ways that share
one ``_apply_lock`` so a queued terminal never lands before its insert:

* an event-driven background drain (woken thread-safely from off-loop writers)
  persists queued mutations promptly; and
* reads flush the queue first, giving read-your-writes consistency.

This mirrors ``IncidentService`` (the off-loop Service template). The persisted
status IS an ``ExecutionState``, so mapping a row to the runtime
``ExecutionRecord`` / ``ExecutionDetail`` is a direct field copy.
"""

import asyncio
import dataclasses
import logging
import queue
from datetime import datetime

from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.execution_record_store import (
    IExecutionRecordStore,
    PersistedExecution,
    PersistedThread,
)
from orca.runtime.status_models import ExecutionDetail, ThreadSnapshot


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
        current_location="",
        current_method=None,
        completed_method_count=0,
        last_error=t.last_error,
        pause_reason=t.pause_reason,
        completed_methods=(),
        labware_id=t.thread_id,
        labware_name=t.name,
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


class ExecutionRecordService:
    """Orchestrates execution-record writes and queries over a per-DB store."""

    def __init__(self, store: IExecutionRecordStore) -> None:
        self._store = store
        self._pending: queue.Queue[_Mutation] = queue.Queue()
        self._apply_lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._stop = False

    def record_running(
        self, execution_id: str, workflow_name: str, submitted_at: datetime
    ) -> None:
        """Record a RUNNING insert. Sync + off-loop safe: enqueues + wakes the drain."""
        self._pending.put(
            _PendingInsert(
                execution_id=execution_id,
                workflow_name=workflow_name,
                submitted_at=submitted_at,
            )
        )
        self._signal()

    def mark_terminal(
        self, execution_id: str, state: ExecutionState, reason: str | None,
        terminal_at: datetime,
    ) -> None:
        """Record a terminal transition. Sync + off-loop safe: enqueues + wakes."""
        self._pending.put(
            _PendingTerminal(
                execution_id=execution_id,
                state=state,
                reason=reason,
                terminal_at=terminal_at,
            )
        )
        self._signal()

    def record_thread(
        self, execution_id: str, *, thread_id: str, name: str,
        template_name: str, status: str,
        last_error: str | None = None, pause_reason: str | None = None,
    ) -> None:
        """Record a thread's latest summary. Sync + off-loop safe: enqueues + wakes."""
        self._pending.put(
            _PendingThread(
                execution_id=execution_id,
                thread=PersistedThread(
                    thread_id=thread_id,
                    name=name,
                    template_name=template_name,
                    status=status,
                    last_error=last_error,
                    pause_reason=pause_reason,
                ),
            )
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

    async def get_record(self, execution_id: str) -> ExecutionRecord | None:
        await self.flush_pending()
        persisted = await self._store.get(execution_id)
        return _to_record(persisted) if persisted is not None else None

    async def list_records(self) -> list[ExecutionRecord]:
        await self.flush_pending()
        return [_to_record(p) for p in await self._store.list_all()]

    async def get_detail(self, execution_id: str) -> ExecutionDetail | None:
        await self.flush_pending()
        persisted = await self._store.get(execution_id)
        if persisted is None:
            return None
        return _to_detail(persisted, await self._store.list_threads(execution_id))

    async def contains(self, execution_id: str) -> bool:
        await self.flush_pending()
        return await self._store.get(execution_id) is not None

    async def list_non_terminal(self) -> list[PersistedExecution]:
        await self.flush_pending()
        return await self._store.list_non_terminal()

    async def mark_interrupted(
        self, execution_id: str, reason: str, terminal_at: datetime
    ) -> None:
        """Boot-scan transition: a row was non-terminal across a restart. The run
        was lost, so it lands as FAILED with the interrupted reason."""
        await self.flush_pending()
        async with self._apply_lock:
            await self._store.mark_terminal(
                execution_id, ExecutionState.FAILED, reason, terminal_at
            )

    async def ensure_schema(self) -> None:
        """Create the store's schema if absent (in-memory/sim setup)."""
        await self._store.create_schema()

    async def flush_pending(self) -> None:
        """Apply every currently-queued mutation, in FIFO order, before returning.

        Taking the lock here is what gives reads their read-your-writes: a read
        that flushes waits out any in-flight drain, so a just-recorded row is
        committed before the caller queries the store."""
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
                logging.getLogger(__name__).warning(
                    "execution-tracking: terminal transition for unknown "
                    "execution_id=%s (state=%s); the SUBMISSION.ACCEPTED "
                    "event may have been lost across a process restart",
                    mutation.execution_id, mutation.state.value,
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
        # Drain to empty: an off-loop straggler (a sink racing teardown) may
        # enqueue as we stop, so loop until nothing remains.
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
