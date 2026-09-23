"""SQLite-backed execution-record store (orca-core source-available persistence).

Dumb persistence behind ``IExecutionRecordStore``: raw async reads/writes, no
locking, no events. The orchestration (sync off-loop writes, the queue + drain,
the DTO->runtime mapping) lives in ``ExecutionRecordService``. A hosted
deployment injects its own store against the same interface; stores share only
the ``IExecutionRecordStore`` interface and the ``PersistedExecution`` DTO, each
keeping its own row model, migration, and engine.

The ``status`` column holds ``ExecutionState.value``. ``upsert_running`` is
first-submission-wins (insert-if-absent); ``mark_terminal`` returns whether a
row was affected.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.models import ExecutionRecordRow, ExecutionThreadRow
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_store import (
    TERMINAL_STATE_VALUES,
    IExecutionRecordStore,
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


class SqliteExecutionRecordStore(IExecutionRecordStore):
    """``IExecutionRecordStore`` over an aiosqlite engine. The database is the
    source of truth; no in-memory copy is kept."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sf = make_session_factory(engine)
        self._schema_ready = False

    async def create_schema(self) -> None:
        if self._schema_ready:
            return
        await create_all_tables(self._engine)
        self._schema_ready = True

    async def upsert_running(
        self, execution_id: str, workflow_name: str, submitted_at: datetime
    ) -> None:
        await self.create_schema()
        async with self._sf() as session:
            existing = await session.get(ExecutionRecordRow, execution_id)
            if existing is not None:
                return
            session.add(
                ExecutionRecordRow(
                    execution_id=execution_id,
                    workflow_name=workflow_name,
                    submitted_at=submitted_at,
                    status=ExecutionState.RUNNING.value,
                )
            )
            await session.commit()

    async def mark_terminal(
        self, execution_id: str, state: ExecutionState, reason: str | None,
        terminal_at: datetime,
    ) -> bool:
        await self.create_schema()
        async with self._sf() as session:
            result = await session.execute(
                update(ExecutionRecordRow)
                .where(ExecutionRecordRow.execution_id == execution_id)
                .values(
                    status=state.value, terminal_at=terminal_at, terminal_reason=reason,
                )
            )
            await session.commit()
        return result.rowcount > 0

    async def get(self, execution_id: str) -> PersistedExecution | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(ExecutionRecordRow, execution_id)
        return _row_to_persisted(row) if row is not None else None

    async def list_all(self) -> list[PersistedExecution]:
        await self.create_schema()
        stmt = select(ExecutionRecordRow).order_by(
            ExecutionRecordRow.submitted_at.desc()
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [_row_to_persisted(row) for row in rows]

    async def upsert_thread(
        self, execution_id: str, thread: PersistedThread,
    ) -> None:
        await self.create_schema()
        async with self._sf() as session:
            existing = await session.get(
                ExecutionThreadRow, (execution_id, thread.thread_id)
            )
            if existing is None:
                session.add(ExecutionThreadRow(
                    execution_id=execution_id,
                    thread_id=thread.thread_id,
                    name=thread.name,
                    template_name=thread.template_name,
                    status=thread.status,
                    last_error=thread.last_error,
                    pause_reason=thread.pause_reason,
                ))
            else:
                existing.name = thread.name
                existing.template_name = thread.template_name
                existing.status = thread.status
                existing.last_error = thread.last_error
                existing.pause_reason = thread.pause_reason
            await session.commit()

    async def list_threads(self, execution_id: str) -> list[PersistedThread]:
        await self.create_schema()
        stmt = (
            select(ExecutionThreadRow)
            .where(ExecutionThreadRow.execution_id == execution_id)
            .order_by(ExecutionThreadRow.thread_id)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [
            PersistedThread(
                thread_id=row.thread_id,
                name=row.name,
                template_name=row.template_name,
                status=row.status,
                last_error=row.last_error,
                pause_reason=row.pause_reason,
            )
            for row in rows
        ]

    async def list_non_terminal(self) -> list[PersistedExecution]:
        await self.create_schema()
        stmt = select(ExecutionRecordRow).where(
            ExecutionRecordRow.status.notin_(list(TERMINAL_STATE_VALUES))
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [_row_to_persisted(row) for row in rows]

    async def aclose(self) -> None:
        await self._engine.dispose()
