"""SQLite-backed incident store (orca-core source-available persistence).

Dumb persistence behind ``IIncidentStore``: raw async reads/writes, no locking,
no events. The orchestration (sync off-loop ``record``, the queue + drain, event
emission) lives in ``IncidentService``. A hosted deployment ships a separate
``PostgresIncidentStore`` against the same interface; the two stores share only
the DB-neutral ``IncidentRow`` model and the mapping helpers, never an engine.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.incident_mapping import incident_to_row, row_to_incident
from orca.runtime.db.models import IncidentRow
from orca.runtime.incident_store import (
    IIncidentStore,
    IncidentCategory,
    SystemIncident,
)


class SqliteIncidentStore(IIncidentStore):
    """``IIncidentStore`` over an aiosqlite engine. The database is the source
    of truth; no in-memory copy is kept."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sf = make_session_factory(engine)
        self._schema_ready = False

    async def create_schema(self) -> None:
        if self._schema_ready:
            return
        await create_all_tables(self._engine)
        self._schema_ready = True

    async def insert(self, incident: SystemIncident) -> None:
        await self.create_schema()
        async with self._sf() as session:
            session.add(incident_to_row(incident))
            await session.commit()

    async def get(self, incident_id: str) -> SystemIncident | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(IncidentRow, incident_id)
        return row_to_incident(row) if row is not None else None

    async def fetch(
        self,
        *,
        unacknowledged_only: bool = False,
        category: IncidentCategory | None = None,
        execution_id: str | None = None,
        since: float | None = None,
    ) -> list[SystemIncident]:
        await self.create_schema()
        stmt = select(IncidentRow)
        if unacknowledged_only:
            stmt = stmt.where(IncidentRow.acknowledged.is_(False))
        if category is not None:
            stmt = stmt.where(IncidentRow.category == category.name)
        if execution_id is not None:
            stmt = stmt.where(IncidentRow.execution_id == execution_id)
        if since is not None:
            stmt = stmt.where(IncidentRow.timestamp >= since)
        stmt = stmt.order_by(IncidentRow.timestamp)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [row_to_incident(row) for row in rows]

    async def mark_acked(self, incident_id: str, acknowledged_at: datetime) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(IncidentRow, incident_id)
            if row is not None:
                row.acknowledged = True
                row.acknowledged_at = acknowledged_at
                await session.commit()

    async def mark_all_acked(
        self, category: IncidentCategory | None, acknowledged_at: datetime
    ) -> int:
        await self.create_schema()
        stmt = select(IncidentRow).where(IncidentRow.acknowledged.is_(False))
        if category is not None:
            stmt = stmt.where(IncidentRow.category == category.name)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
            for row in rows:
                row.acknowledged = True
                row.acknowledged_at = acknowledged_at
            await session.commit()
        return len(rows)

    async def aclose(self) -> None:
        await self._engine.dispose()
