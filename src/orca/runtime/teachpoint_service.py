"""TeachpointService: DB-agnostic orchestration over an ``ITeachpointStore``.

A teachpoint registry is per-transporter and on-loop-only: operator CRUD and
runtime resolution both run on the event loop, so there is no off-loop writer
and none of the incident vertical's queue/drain machinery. Every method awaits
the store directly under one ``asyncio.Lock`` so concurrent mutations serialize.
The Service itself satisfies ``ITeachpointStore``, so the Transporter and
runtime resolution consume it as a drop-in store.

The Service owns the authoring rule: ``add`` and ``update`` reject teachpoints
that carry inline access fields without a named ``AccessConfig`` via
``validate_persistable_access`` before delegating the raw write to the store.

No domain event is emitted: there is no teachpoint RuntimeEvent type today, so
the on-record emission seam the incident vertical uses is intentionally absent.
"""

import asyncio
from typing import List, Protocol

from cheshire_drivers.teachpoints import Teachpoint

from orca.resource_models.store_interfaces import ITeachpointStore
from orca.runtime.teachpoint_store import validate_persistable_access


class ITeachpointService(ITeachpointStore, Protocol):
    """One device's teachpoints, one at a time under one lock. It is used wherever a store is."""


class TeachpointService:
    """Orchestrates teachpoint CRUD over a per-DB store under one lock."""

    def __init__(self, store: ITeachpointStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def get(self, position_id: str) -> Teachpoint | None:
        async with self._lock:
            return await self._store.get(position_id)

    async def resolve(self, position_id: str) -> Teachpoint | None:
        async with self._lock:
            return await self._store.resolve(position_id)

    async def list(self) -> List[Teachpoint]:
        async with self._lock:
            return await self._store.list()

    async def add(self, teachpoint: Teachpoint) -> None:
        validate_persistable_access(teachpoint)
        async with self._lock:
            await self._store.add(teachpoint)

    async def update(self, teachpoint: Teachpoint) -> None:
        validate_persistable_access(teachpoint)
        async with self._lock:
            await self._store.update(teachpoint)

    async def delete(self, position_id: str) -> bool:
        async with self._lock:
            return await self._store.delete(position_id)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies ITeachpointStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()
