"""The incident store over SQLite. Its reads and writes are in ``orca.runtime.db.incident_queries``.

Dumb persistence behind ``IIncidentStore``: no locking, no events. The sync
off-loop ``record``, the queue and drain, and event emission live in
``IncidentService``.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, incident_queries, make_session_factory
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
            await incident_queries.insert(session, incident)

    async def get(self, incident_id: str) -> SystemIncident | None:
        await self.create_schema()
        async with self._sf() as session:
            return await incident_queries.get(session, incident_id)

    async def fetch(
        self,
        *,
        unacknowledged_only: bool = False,
        category: IncidentCategory | None = None,
        execution_id: str | None = None,
        since: float | None = None,
    ) -> list[SystemIncident]:
        await self.create_schema()
        async with self._sf() as session:
            return await incident_queries.fetch(
                session, unacknowledged_only=unacknowledged_only, category=category,
                execution_id=execution_id, since=since,
            )

    async def mark_acked(self, incident_id: str, acknowledged_at: datetime) -> None:
        await self.create_schema()
        async with self._sf() as session:
            await incident_queries.mark_acked(session, incident_id, acknowledged_at)

    async def mark_all_acked(
        self, category: IncidentCategory | None, acknowledged_at: datetime
    ) -> int:
        await self.create_schema()
        async with self._sf() as session:
            return await incident_queries.mark_all_acked(session, category, acknowledged_at)

    async def aclose(self) -> None:
        await self._engine.dispose()
