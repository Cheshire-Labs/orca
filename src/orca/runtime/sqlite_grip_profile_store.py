"""SQLite-backed grip-profile store (orca-core source-available persistence).

Dumb persistence behind ``IGripProfileStore``: raw async reads and writes, no
locking, no events. The orchestration (single-lock access, schema lifecycle)
lives in ``GripProfileService``. A hosted deployment ships a Postgres store
against the same interface; the two share only the DB-neutral row model and the
mapping helpers.
"""

from cheshire_drivers.move_parameters import MoveParameterPatch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.grip_profile_mapping import (
    grip_profile_to_row,
    row_to_grip_profile,
)
from orca.runtime.db.models import GripProfileRow


class SqliteGripProfileStore:
    """``IGripProfileStore`` over an aiosqlite engine. The database is the
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

    async def get(self, labware_type: str) -> MoveParameterPatch | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(GripProfileRow, labware_type)
        return row_to_grip_profile(row) if row is not None else None

    async def list(self) -> dict[str, MoveParameterPatch]:
        await self.create_schema()
        stmt = select(GripProfileRow).order_by(GripProfileRow.labware_type)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return {row.labware_type: row_to_grip_profile(row) for row in rows}

    async def set(self, labware_type: str, patch: MoveParameterPatch) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(GripProfileRow, labware_type)
            if row is None:
                session.add(grip_profile_to_row(labware_type, patch))
            else:
                row.patch = patch.model_dump(exclude_none=True)
            await session.commit()

    async def delete(self, labware_type: str) -> bool:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(GripProfileRow, labware_type)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def aclose(self) -> None:
        await self._engine.dispose()
