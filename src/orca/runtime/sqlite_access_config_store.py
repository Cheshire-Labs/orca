"""SQLite-backed access-config store (orca-core source-available persistence).

Dumb persistence behind ``IAccessConfigStore``: raw async reads/writes, no
locking, no events. The orchestration (single-lock CRUD, schema lifecycle)
lives in ``AccessConfigService``. A hosted deployment ships a separate
``PostgresAccessConfigStore`` against the same interface; the two stores share
only the DB-neutral ``AccessConfigRow`` model and the mapping helpers.
"""

from typing import List

from cheshire_drivers.teachpoints import AccessConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.access_config_mapping import (
    access_config_to_row,
    apply_access_config_to_row,
    row_to_access_config,
)
from orca.runtime.db.models import AccessConfigRow


class SqliteAccessConfigStore:
    """``IAccessConfigStore`` over an aiosqlite engine. The database is the
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

    async def get(self, name: str) -> AccessConfig | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(AccessConfigRow, name)
        return row_to_access_config(row) if row is not None else None

    async def list(self) -> List[AccessConfig]:
        await self.create_schema()
        stmt = select(AccessConfigRow).order_by(AccessConfigRow.name)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [row_to_access_config(row) for row in rows]

    async def add(self, config: AccessConfig) -> None:
        await self.create_schema()
        async with self._sf() as session:
            if await session.get(AccessConfigRow, config.name) is not None:
                raise ValueError(f"AccessConfig {config.name!r} already registered")
            session.add(access_config_to_row(config))
            await session.commit()

    async def update(self, config: AccessConfig) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(AccessConfigRow, config.name)
            if row is None:
                raise KeyError(f"AccessConfig {config.name!r} not found")
            apply_access_config_to_row(row, config)
            await session.commit()

    async def delete(self, name: str) -> bool:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(AccessConfigRow, name)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def aclose(self) -> None:
        await self._engine.dispose()
