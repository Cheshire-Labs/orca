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
from typing import Iterable, List

from cheshire_drivers.teachpoints import Teachpoint

from orca.runtime.db import create_memory_engine
from orca.runtime.interfaces import ITeachpointStore
from orca.runtime.sqlite_teachpoint_store import SqliteTeachpointStore
from orca.runtime.teachpoint_store import validate_persistable_access


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

    async def seed_if_missing(self, teachpoints: Iterable[Teachpoint]) -> None:
        """Insert each teachpoint whose position_id is not present; never overwrite.

        Lets a transporter's registry outlive multiple topology mounts: each
        mount re-declares its seed and existing rows (operator edits included)
        win over the re-declared seed. Seeds are validated like any write.
        """
        async with self._lock:
            for teachpoint in teachpoints:
                validate_persistable_access(teachpoint)
                if await self._store.get(teachpoint.position_id) is None:
                    await self._store.add(teachpoint)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies ITeachpointStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()


class _LazySeededTeachpointService(TeachpointService):
    """TeachpointService whose seed is reconciled on first async access.

    For sync construction sites already on a running loop (topology ``build``,
    test helpers) that cannot await the seed write. The seed is resolved
    synchronously and reconciled into the store on the first CRUD call.
    """

    def __init__(self, store: ITeachpointStore, seed: List[Teachpoint]) -> None:
        super().__init__(store)
        self._pending_seed: List[Teachpoint] = list(seed)
        self._seeded = False

    async def _ensure_seeded(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        if self._pending_seed:
            await self.seed_if_missing(self._pending_seed)

    async def get(self, position_id: str) -> Teachpoint | None:
        await self._ensure_seeded()
        return await super().get(position_id)

    async def resolve(self, position_id: str) -> Teachpoint | None:
        await self._ensure_seeded()
        return await super().resolve(position_id)

    async def list(self) -> List[Teachpoint]:
        await self._ensure_seeded()
        return await super().list()

    async def add(self, teachpoint: Teachpoint) -> None:
        await self._ensure_seeded()
        await super().add(teachpoint)

    async def update(self, teachpoint: Teachpoint) -> None:
        await self._ensure_seeded()
        await super().update(teachpoint)

    async def delete(self, position_id: str) -> bool:
        await self._ensure_seeded()
        return await super().delete(position_id)


def seeded_teachpoint_service(
    seed: Iterable[Teachpoint] = (),
) -> TeachpointService:
    """A SQLite-backed TeachpointService seeded lazily on first async access.

    Drop-in for sync construction sites that previously built an in-memory
    teachpoint store with an immediate seed.
    """
    return _LazySeededTeachpointService(
        SqliteTeachpointStore(create_memory_engine()), list(seed),
    )
