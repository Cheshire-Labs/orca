"""Execution-record domain types + the per-DB store interface.

One row per submitted workflow execution, tracking the ``ExecutionState`` of its
asyncio task (RUNNING -> a terminal outcome). This module holds the persisted
DTO (``PersistedExecution``) and ``IExecutionRecordStore`` (the dumb per-DB
persistence interface). The orchestration -- sync off-loop writes, the queue +
drain, and the DTO->runtime mapping -- lives in ``ExecutionRecordService``;
orca-core ships ``SqliteExecutionRecordStore`` and a hosted deployment injects
its own store, both satisfying this interface.

Mid-execution resume is NOT supported (the per-thread async-generator position
cannot be serialized); the table exists so operator-facing status is honest
after a restart.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from orca.runtime.execution_record import ExecutionState


# The DB stores ExecutionState.value; the boot scan reconciles non-terminal rows
# (only RUNNING today). Derived from the enum so a new state is covered for free.
TERMINAL_STATE_VALUES = frozenset(s.value for s in ExecutionState if s.is_terminal())


@dataclass(frozen=True)
class PersistedExecution:
    """One persisted execution row: identity + ExecutionState + terminal facts."""

    execution_id: str
    workflow_name: str
    submitted_at: datetime
    status: ExecutionState
    terminal_at: datetime | None = None
    terminal_reason: str | None = None


@dataclass(frozen=True)
class PersistedThread:
    """One thread's last-known summary, upserted from THREAD lifecycle events.

    What survives a restart so ``get_execution_detail`` can still say what a
    finished run did. Location/method progress are not here; the ops history
    holds the full action trail.
    """

    thread_id: str
    name: str
    template_name: str
    status: str
    last_error: str | None = None
    pause_reason: str | None = None


class IExecutionRecordStore(Protocol):
    """Dumb per-DB persistence for execution records.

    Raw async reads/writes, no locking, no events (those live in
    ``ExecutionRecordService``). orca-core ships ``SqliteExecutionRecordStore``;
    a hosted deployment injects its own store; both satisfy this interface and
    the Service is constructed with whichever is injected.
    """

    async def upsert_running(
        self, execution_id: str, workflow_name: str, submitted_at: datetime
    ) -> None:
        """Insert a RUNNING row if absent; first-submission-wins (no overwrite)."""
        ...

    async def mark_terminal(
        self, execution_id: str, state: ExecutionState, reason: str | None,
        terminal_at: datetime,
    ) -> bool:
        """Set the terminal state/at/reason. Returns True iff a row was affected."""
        ...

    async def get(self, execution_id: str) -> PersistedExecution | None: ...

    async def upsert_thread(
        self, execution_id: str, thread: PersistedThread,
    ) -> None:
        """Insert or replace one thread's summary under its execution."""
        ...

    async def list_threads(self, execution_id: str) -> list[PersistedThread]:
        """Every thread summary recorded for the execution, by thread_id."""
        ...

    async def list_all(self) -> list[PersistedExecution]:
        """Every persisted row, newest-first by submitted_at."""
        ...

    async def list_non_terminal(self) -> list[PersistedExecution]:
        """Rows still in a non-terminal state (boot-scan input)."""
        ...

    async def create_schema(self) -> None:
        """Create the executions table if absent (in-memory/sim setup; file and
        Postgres deployments use migrations, where this is an idempotent no-op)."""
        ...

    async def aclose(self) -> None:
        """Release the store's resources (e.g. dispose the DB engine)."""
        ...
