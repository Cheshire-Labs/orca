"""DeckLayoutService: DB-agnostic orchestration over an ``IDeckLayoutStore``.

A deck-layout registry is per-liquid-handler and on-loop-only: operator CRUD
and the build-time resolver both run on the event loop, so there is no
off-loop writer and none of the incident vertical's queue/drain machinery.
Every method awaits the store directly under one ``asyncio.Lock`` so
concurrent mutations serialize. The Service itself satisfies
``IDeckLayoutStore``, so the LiquidHandler and the facade consume it as a
drop-in store.

Deck layouts carry no inline-access authoring rule (unlike teachpoints), so
``add``/``update`` delegate straight to the store with no validation.

No domain event is emitted: there is no deck-layout RuntimeEvent type today, so
the on-record emission seam the incident vertical uses is intentionally absent.
"""

import asyncio
from typing import List, Protocol, Tuple

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig

from orca.resource_models.store_interfaces import IDeckLayoutStore


class IDeckLayoutService(IDeckLayoutStore, Protocol):
    """One liquid handler's deck layouts, one at a time under one lock. It is used wherever a store is."""


class DeckLayoutService:
    """Orchestrates deck-layout CRUD over a per-DB store under one lock."""

    def __init__(self, store: IDeckLayoutStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> DeckLayoutConfig | None:
        async with self._lock:
            return await self._store.get(name)

    async def list(self) -> List[Tuple[str, DeckLayoutConfig]]:
        async with self._lock:
            return await self._store.list()

    async def add(self, name: str, config: DeckLayoutConfig) -> None:
        async with self._lock:
            await self._store.add(name, config)

    async def update(self, name: str, config: DeckLayoutConfig) -> None:
        async with self._lock:
            await self._store.update(name, config)

    async def delete(self, name: str) -> bool:
        async with self._lock:
            return await self._store.delete(name)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies IDeckLayoutStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()
