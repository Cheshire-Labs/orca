"""The execution-record store over SQLite. Its reads and writes are in
``orca.runtime.db.execution_record_queries``.

Dumb persistence behind ``IExecutionRecordStore``: no locking, no events. The
sync off-loop writes, the queue and drain, and the DTO-to-runtime mapping live
in ``ExecutionRecordService``.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, execution_record_queries, make_session_factory
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_store import (
    IExecutionRecordStore,
    PersistedExecution,
    PersistedThread,
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
            await execution_record_queries.upsert_running(session, execution_id, workflow_name, submitted_at)

    async def mark_terminal(
        self, execution_id: str, state: ExecutionState, reason: str | None,
        terminal_at: datetime,
    ) -> bool:
        await self.create_schema()
        async with self._sf() as session:
            return await execution_record_queries.mark_terminal(session, execution_id, state, reason, terminal_at)

    async def get(self, execution_id: str) -> PersistedExecution | None:
        await self.create_schema()
        async with self._sf() as session:
            return await execution_record_queries.get(session, execution_id)

    async def list_all(self) -> list[PersistedExecution]:
        await self.create_schema()
        async with self._sf() as session:
            return await execution_record_queries.list_all(session)

    async def upsert_thread(
        self, execution_id: str, thread: PersistedThread,
    ) -> None:
        await self.create_schema()
        async with self._sf() as session:
            await execution_record_queries.upsert_thread(session, execution_id, thread)

    async def list_threads(self, execution_id: str) -> list[PersistedThread]:
        await self.create_schema()
        async with self._sf() as session:
            return await execution_record_queries.list_threads(session, execution_id)

    async def list_non_terminal(self) -> list[PersistedExecution]:
        await self.create_schema()
        async with self._sf() as session:
            return await execution_record_queries.list_non_terminal(session)

    async def delete(self, execution_id: str) -> None:
        await self.create_schema()
        async with self._sf() as session:
            await execution_record_queries.delete(session, execution_id)

    async def aclose(self) -> None:
        await self._engine.dispose()
