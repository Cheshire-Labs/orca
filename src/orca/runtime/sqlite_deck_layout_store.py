"""SQLite-backed deck-layout store (orca-core source-available persistence).

Dumb persistence behind ``IDeckLayoutStore``: raw async reads/writes, no
locking. The orchestration (single-lock CRUD, schema lifecycle) lives in
``DeckLayoutService``. A hosted deployment ships a separate ``PostgresDeckLayoutStore``
against the same interface; the two stores share only the DB-neutral
``DeckLayoutRow`` model and the mapping helpers.

The store is per-liquid-handler: ``name`` is the sole key, the store
instance is the device scope. The whole config is persisted as one JSON
column so a layout round-trips without per-field flattening.
"""

from typing import List, Tuple

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_all_tables, make_session_factory
from orca.runtime.db.deck_layout_mapping import (
    apply_deck_layout_to_row,
    deck_layout_to_row,
    row_to_deck_layout,
)
from orca.runtime.db.models import DeckLayoutRow


class SqliteDeckLayoutStore:
    """``IDeckLayoutStore`` over an aiosqlite engine. The database is the
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

    async def get(self, name: str) -> DeckLayoutConfig | None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(DeckLayoutRow, name)
        return row_to_deck_layout(row)[1] if row is not None else None

    async def list(self) -> List[Tuple[str, DeckLayoutConfig]]:
        await self.create_schema()
        stmt = select(DeckLayoutRow).order_by(DeckLayoutRow.name)
        async with self._sf() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [row_to_deck_layout(row) for row in rows]

    async def add(self, name: str, config: DeckLayoutConfig) -> None:
        await self.create_schema()
        async with self._sf() as session:
            if await session.get(DeckLayoutRow, name) is not None:
                raise ValueError(f"DeckLayout {name!r} already registered")
            session.add(deck_layout_to_row(name, config))
            await session.commit()

    async def update(self, name: str, config: DeckLayoutConfig) -> None:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(DeckLayoutRow, name)
            if row is None:
                raise KeyError(f"DeckLayout {name!r} not found")
            apply_deck_layout_to_row(row, config)
            await session.commit()

    async def delete(self, name: str) -> bool:
        await self.create_schema()
        async with self._sf() as session:
            row = await session.get(DeckLayoutRow, name)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def aclose(self) -> None:
        await self._engine.dispose()
