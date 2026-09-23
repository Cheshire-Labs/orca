"""GripProfileService: DB-agnostic orchestration over an ``IGripProfileStore``.

A profile is read on every move that carries a labware and written rarely, both
on the event loop, so this is one ``asyncio.Lock`` over the store and nothing
else. The Service itself satisfies ``IGripProfileStore``, so a facade and the
transporter consume it as a drop-in store.

``seed_if_missing`` is what lets a topology declare a starting point for the
labware types it ships without overwriting what an operator has since measured:
the stored row always wins over a re-declared seed, the same contract the access
configs and the move defaults have.
"""

import asyncio
from collections.abc import Iterable

from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch

from orca.runtime.interfaces import IGripProfileStore


class GripProfileService:
    """Orchestrates grip-profile reads and writes over a per-DB store."""

    def __init__(self, store: IGripProfileStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def get(self, labware_type: str) -> MoveParameterPatch | None:
        async with self._lock:
            return await self._store.get(labware_type)

    async def list(self) -> dict[str, MoveParameterPatch]:
        async with self._lock:
            return await self._store.list()

    async def set(self, labware_type: str, patch: MoveParameterPatch) -> None:
        async with self._lock:
            await self._store.set(labware_type, patch)

    async def delete(self, labware_type: str) -> bool:
        async with self._lock:
            return await self._store.delete(labware_type)

    async def apply(
        self,
        labware_type: str,
        patch: MoveParameterPatch,
        clear: Iterable[MoveParameterField] = (),
    ) -> MoveParameterPatch:
        """Merge an edit into the stored profile and return what it now holds.

        Setting and clearing in one call for the same reason the move defaults
        do it: correcting a grip width while handing the jaw opening back to the
        arm is one decision, and two writes would leave a move in between them
        resolving against a mixture neither call intended.

        A profile that ends up naming nothing is deleted rather than stored
        empty, so "this type has no opinion" has exactly one representation.
        """
        async with self._lock:
            stored = await self._store.get(labware_type) or MoveParameterPatch()
            merged = patch.over(stored).without(clear)
            if merged.model_dump(exclude_none=True):
                await self._store.set(labware_type, merged)
            else:
                await self._store.delete(labware_type)
            return merged

    async def seed_if_missing(
        self, labware_type: str, patch: MoveParameterPatch,
    ) -> None:
        """Write the profile only if the type has none; never overwrite."""
        async with self._lock:
            if await self._store.get(labware_type) is None:
                await self._store.set(labware_type, patch)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies IGripProfileStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()
