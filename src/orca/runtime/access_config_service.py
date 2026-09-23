"""AccessConfigService: DB-agnostic orchestration over an ``IAccessConfigStore``.

Access configs are a deployment-global, on-loop-only registry: operator CRUD and
runtime resolution both run on the event loop, so there is no off-loop writer and
none of the incident vertical's queue/drain machinery. Every method awaits the
store directly under one ``asyncio.Lock`` so concurrent mutations serialize. The
Service itself satisfies ``IAccessConfigStore``, so the facade, the teachpoint
store, and runtime resolution consume it as a drop-in store.

No domain event is emitted: there is no access-config RuntimeEvent type today, so
the on-record emission seam the incident vertical uses is intentionally absent.
"""

import asyncio
from typing import Iterable, List

from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.interfaces import IAccessConfigStore


class AccessConfigService:
    """Orchestrates access-config CRUD over a per-DB store under one lock."""

    def __init__(self, store: IAccessConfigStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> AccessConfig | None:
        async with self._lock:
            return await self._store.get(name)

    async def list(self) -> List[AccessConfig]:
        async with self._lock:
            return await self._store.list()

    async def add(self, config: AccessConfig) -> None:
        async with self._lock:
            await self._store.add(config)

    async def update(self, config: AccessConfig) -> None:
        async with self._lock:
            await self._store.update(config)

    async def delete(self, name: str) -> bool:
        async with self._lock:
            return await self._store.delete(name)

    async def seed_if_missing(self, configs: Iterable[AccessConfig]) -> None:
        """Insert each config whose name is not already present; never overwrite.

        Lets one deployment-wide registry outlive multiple topology mounts: each
        mount re-declares its seed and existing rows (operator edits included)
        win over the re-declared seed.
        """
        async with self._lock:
            for config in configs:
                if await self._store.get(config.name) is None:
                    await self._store.add(config)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies IAccessConfigStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()
