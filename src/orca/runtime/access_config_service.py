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
from typing import List, Protocol

from cheshire_drivers.teachpoints import AccessConfig

from orca.resource_models.store_interfaces import IAccessConfigStore


class IAccessConfigService(IAccessConfigStore, Protocol):
    """Access configs, one at a time under one lock. It is used wherever a store is."""


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

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies IAccessConfigStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()
