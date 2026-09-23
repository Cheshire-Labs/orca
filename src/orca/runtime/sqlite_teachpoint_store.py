"""SQLite-backed teachpoint store (orca-core source-available persistence).

Dumb persistence behind ``ITeachpointStore``: raw async reads/writes, no
locking, no authoring validation. The orchestration (single-lock CRUD,
``validate_persistable_access``, schema lifecycle) lives in
``TeachpointService``. A hosted deployment ships a separate ``PostgresTeachpointStore``
against the same interface; the two stores share only the DB-neutral
``TeachpointRow`` model and the mapping helpers.

The store is per-transporter: ``position_id`` is the sole key, the store
instance is the device scope. Access fields are persisted inline so a
teachpoint round-trips without an AccessConfig lookup.
"""

from typing import List

from cheshire_drivers.teachpoints import Teachpoint
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.models import TeachpointRow
from orca.runtime.db.teachpoint_mapping import (
    apply_teachpoint_to_row,
    row_to_teachpoint,
    teachpoint_to_row,
)


class SqliteTeachpointStore:
    """``ITeachpointStore`` over an aiosqlite engine. The database is the
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

    async def get(self, position_id: str) -> Teachpoint | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(TeachpointRow, position_id)
        return row_to_teachpoint(row) if row is not None else None

    async def resolve(self, position_id: str) -> Teachpoint | None:
        """Wire-source-of-truth lookup; equivalent to ``get`` for this store.

        The persisted access fields are inlined on reconstruction, so the
        returned teachpoint is already wire-ready.
        """
        return await self.get(position_id)

    async def list(self) -> List[Teachpoint]:
        await self.create_schema()
        stmt = select(TeachpointRow).order_by(TeachpointRow.position_id)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [row_to_teachpoint(row) for row in rows]

    async def add(self, teachpoint: Teachpoint) -> None:
        await self.create_schema()
        async with self._sf() as session:
            if await session.get(TeachpointRow, teachpoint.position_id) is not None:
                raise ValueError(
                    f"Teachpoint {teachpoint.position_id!r} already registered"
                )
            session.add(teachpoint_to_row(teachpoint))
            await session.commit()

    async def update(self, teachpoint: Teachpoint) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(TeachpointRow, teachpoint.position_id)
            if row is None:
                raise KeyError(f"Teachpoint {teachpoint.position_id!r} not found")
            apply_teachpoint_to_row(row, teachpoint)
            await session.commit()

    async def delete(self, position_id: str) -> bool:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(TeachpointRow, position_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def aclose(self) -> None:
        await self._engine.dispose()
