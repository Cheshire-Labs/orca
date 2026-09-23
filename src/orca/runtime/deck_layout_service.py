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
from typing import List, Mapping, Optional, Tuple

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig

from orca.runtime.db import create_memory_engine
from orca.runtime.interfaces import IDeckLayoutStore
from orca.runtime.sqlite_deck_layout_store import SqliteDeckLayoutStore


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

    async def seed_if_missing(
        self, configs: Mapping[str, DeckLayoutConfig],
    ) -> None:
        """Insert each layout whose name is not present; never overwrite.

        Lets a liquid handler's registry outlive multiple topology mounts: each
        mount re-declares its seed and existing rows (operator edits included)
        win over the re-declared seed.
        """
        async with self._lock:
            for name, config in configs.items():
                if await self._store.get(name) is None:
                    await self._store.add(name, config)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies IDeckLayoutStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()


class _LazySeededDeckLayoutService(DeckLayoutService):
    """DeckLayoutService whose seed is reconciled on first async access.

    For sync construction sites already on a running loop (topology ``build``,
    test helpers) that cannot await the seed write. The seed is resolved
    synchronously and reconciled into the store on the first CRUD call.
    """

    def __init__(
        self,
        store: IDeckLayoutStore,
        seed: Mapping[str, DeckLayoutConfig],
    ) -> None:
        super().__init__(store)
        self._pending_seed: dict[str, DeckLayoutConfig] = dict(seed)
        self._seeded = False

    async def _ensure_seeded(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        if self._pending_seed:
            await self.seed_if_missing(self._pending_seed)

    async def get(self, name: str) -> DeckLayoutConfig | None:
        await self._ensure_seeded()
        return await super().get(name)

    async def list(self) -> List[Tuple[str, DeckLayoutConfig]]:
        await self._ensure_seeded()
        return await super().list()

    async def add(self, name: str, config: DeckLayoutConfig) -> None:
        await self._ensure_seeded()
        await super().add(name, config)

    async def update(self, name: str, config: DeckLayoutConfig) -> None:
        await self._ensure_seeded()
        await super().update(name, config)

    async def delete(self, name: str) -> bool:
        await self._ensure_seeded()
        return await super().delete(name)


def seeded_deck_layout_service(
    seed: Optional[Mapping[str, DeckLayoutConfig]] = None,
) -> DeckLayoutService:
    """A SQLite-backed DeckLayoutService seeded lazily on first async access.

    Drop-in for sync construction sites that previously built an in-memory
    deck-layout store with an immediate seed.
    """
    return _LazySeededDeckLayoutService(
        SqliteDeckLayoutStore(create_memory_engine()), dict(seed or {}),
    )
