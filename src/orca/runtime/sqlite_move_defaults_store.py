"""SQLite-backed move-defaults store (orca-core source-available persistence).

Dumb persistence behind ``IMoveDefaultsStore``: raw async reads and writes, no
locking, no events. The orchestration (single-lock access, schema lifecycle,
seed-if-missing) lives in ``MoveDefaultsService``. A hosted deployment ships a
Postgres store against the same interface; the two share only the DB-neutral
row model and the mapping helpers.
"""

from cheshire_drivers.move_parameters import MoveParameterPatch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.models import MoveDefaultsRow
from orca.runtime.db.move_defaults_mapping import (
    move_defaults_to_row,
    row_to_move_defaults,
)


class SqliteMoveDefaultsStore:
    """``IMoveDefaultsStore`` over an aiosqlite engine. The database is the
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

    async def get(self, transporter_name: str) -> MoveParameterPatch | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(MoveDefaultsRow, transporter_name)
        return row_to_move_defaults(row) if row is not None else None

    async def list(self) -> dict[str, MoveParameterPatch]:
        await self.create_schema()
        stmt = select(MoveDefaultsRow).order_by(MoveDefaultsRow.transporter_name)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return {row.transporter_name: row_to_move_defaults(row) for row in rows}

    async def set(self, transporter_name: str, patch: MoveParameterPatch) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(MoveDefaultsRow, transporter_name)
            if row is None:
                session.add(move_defaults_to_row(transporter_name, patch))
            else:
                row.patch = patch.model_dump(exclude_none=True)
            await session.commit()

    async def delete(self, transporter_name: str) -> bool:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(MoveDefaultsRow, transporter_name)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def aclose(self) -> None:
        await self._engine.dispose()
