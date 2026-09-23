"""SQLite-backed labware-catalog store (orca-core source-available persistence).

Dumb persistence behind ``ILabwareCatalogStore``: raw async reads/writes, no
locking and no policy. The orchestration (single-lock CRUD, force-custom,
read-only-seed, geometry validation) lives in ``LabwareCatalogService``. A hosted
deployment ships a separate DB-backed store against the same interface; the two stores share
only the DB-neutral ``LabwareCatalogRow`` model and the mapping helpers.

``put`` is an upsert keyed by ``labware_type``; ``delete`` is idempotent.
"""

from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.labware_catalog_mapping import (
    apply_catalog_entry_to_row,
    catalog_entry_to_row,
    row_to_catalog_entry,
)
from orca.runtime.db.models import LabwareCatalogRow
from orca.runtime.labware_catalog_store import LabwareCatalogEntry


class SqliteLabwareCatalogStore:
    """``ILabwareCatalogStore`` over an aiosqlite engine. The database is the
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

    async def list(
        self, category: str | None = None,
    ) -> List[LabwareCatalogEntry]:
        await self.create_schema()
        stmt = select(LabwareCatalogRow).order_by(LabwareCatalogRow.labware_type)
        if category is not None:
            stmt = stmt.where(LabwareCatalogRow.category == category)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [row_to_catalog_entry(row) for row in rows]

    async def get(self, labware_type: str) -> LabwareCatalogEntry | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(LabwareCatalogRow, labware_type)
        return row_to_catalog_entry(row) if row is not None else None

    async def put(self, entry: LabwareCatalogEntry) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(LabwareCatalogRow, entry.labware_type)
            if row is None:
                session.add(catalog_entry_to_row(entry))
            else:
                apply_catalog_entry_to_row(row, entry)
            await session.commit()

    async def delete(self, labware_type: str) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(LabwareCatalogRow, labware_type)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def aclose(self) -> None:
        await self._engine.dispose()
