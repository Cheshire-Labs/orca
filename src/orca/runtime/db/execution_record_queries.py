"""The execution-record store's reads and writes, for any database.

The ``status`` column holds ``ExecutionState.value``. Times are stored as naive UTC.
"""

from datetime import datetime

from sqlalchemy import delete as delete_rows
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orca.runtime.db.base import naive_utc
from orca.runtime.db.models import ExecutionRecordRow, ExecutionThreadRow
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_store import (
    TERMINAL_STATE_VALUES,
    PersistedExecution,
    PersistedThread,
)


def _row_to_persisted(row: ExecutionRecordRow) -> PersistedExecution:
    return PersistedExecution(
        execution_id=row.execution_id,
        workflow_name=row.workflow_name,
        submitted_at=row.submitted_at,
        status=ExecutionState(row.status),
        terminal_at=row.terminal_at,
        terminal_reason=row.terminal_reason,
    )


def _row_to_thread(row: ExecutionThreadRow) -> PersistedThread:
    return PersistedThread(
        thread_id=row.thread_id,
        name=row.name,
        template_name=row.template_name,
        status=row.status,
        last_error=row.last_error,
        pause_reason=row.pause_reason,
        group_id=row.group_id,
    )


async def upsert_running(
    session: AsyncSession, execution_id: str, workflow_name: str, submitted_at: datetime,
) -> None:
    """Insert a RUNNING row unless one exists; the first submission wins."""
    if await session.get(ExecutionRecordRow, execution_id) is not None:
        return
    session.add(
        ExecutionRecordRow(
            execution_id=execution_id,
            workflow_name=workflow_name,
            submitted_at=naive_utc(submitted_at),
            status=ExecutionState.RUNNING.value,
        )
    )
    await session.commit()


async def mark_terminal(
    session: AsyncSession, execution_id: str, state: ExecutionState, reason: str | None,
    terminal_at: datetime,
) -> bool:
    """Set the terminal state, time and reason. True when the row exists."""
    # Read then set, not UPDATE ... RETURNING: SQLite before 3.35 has no RETURNING.
    row = await session.get(ExecutionRecordRow, execution_id)
    if row is None:
        return False
    row.status = state.value
    row.terminal_at = naive_utc(terminal_at)
    row.terminal_reason = reason
    await session.commit()
    return True


async def get(session: AsyncSession, execution_id: str) -> PersistedExecution | None:
    row = await session.get(ExecutionRecordRow, execution_id)
    return None if row is None else _row_to_persisted(row)


async def list_all(session: AsyncSession) -> list[PersistedExecution]:
    """Every row, newest submission first."""
    rows = (await session.execute(
        select(ExecutionRecordRow).order_by(ExecutionRecordRow.submitted_at.desc())
    )).scalars().all()
    return [_row_to_persisted(row) for row in rows]


async def upsert_thread(session: AsyncSession, execution_id: str, thread: PersistedThread) -> None:
    existing = await session.get(ExecutionThreadRow, (execution_id, thread.thread_id))
    if existing is None:
        session.add(ExecutionThreadRow(
            execution_id=execution_id,
            thread_id=thread.thread_id,
            name=thread.name,
            template_name=thread.template_name,
            status=thread.status,
            last_error=thread.last_error,
            pause_reason=thread.pause_reason,
            group_id=thread.group_id,
        ))
    else:
        existing.name = thread.name
        existing.template_name = thread.template_name
        existing.status = thread.status
        existing.last_error = thread.last_error
        existing.pause_reason = thread.pause_reason
        existing.group_id = thread.group_id
    await session.commit()


async def list_threads(session: AsyncSession, execution_id: str) -> list[PersistedThread]:
    """Every thread summary of the execution, by thread_id."""
    rows = (await session.execute(
        select(ExecutionThreadRow)
        .where(ExecutionThreadRow.execution_id == execution_id)
        .order_by(ExecutionThreadRow.thread_id)
    )).scalars().all()
    return [_row_to_thread(row) for row in rows]


async def list_non_terminal(session: AsyncSession) -> list[PersistedExecution]:
    rows = (await session.execute(
        select(ExecutionRecordRow).where(ExecutionRecordRow.status.notin_(list(TERMINAL_STATE_VALUES)))
    )).scalars().all()
    return [_row_to_persisted(row) for row in rows]


async def delete(session: AsyncSession, execution_id: str) -> None:
    """Remove the run's row and its threads."""
    await session.execute(delete_rows(ExecutionThreadRow).where(ExecutionThreadRow.execution_id == execution_id))
    await session.execute(delete_rows(ExecutionRecordRow).where(ExecutionRecordRow.execution_id == execution_id))
    await session.commit()
