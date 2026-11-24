from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.audit_store import AuditEntry, AuditQuery, IAuditStore
from orca.runtime.db import Base, audit_queries, create_tables, make_session_factory
from orca.runtime.db.models import AuditEntryRow


class SqliteAuditStore(IAuditStore):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sf = make_session_factory(engine)
        self._schema_ready = False

    async def _create_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(create_tables, [Base.metadata.tables[AuditEntryRow.__tablename__]])
        self._schema_ready = True

    async def append(self, entry: AuditEntry) -> None:
        await self._create_schema()
        async with self._sf() as session:
            await audit_queries.append(session, entry)

    async def newest_first(self, query: AuditQuery) -> list[AuditEntry]:
        await self._create_schema()
        async with self._sf() as session:
            return await audit_queries.newest_first(session, query)
