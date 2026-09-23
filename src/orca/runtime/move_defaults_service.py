"""MoveDefaultsService: DB-agnostic orchestration over an ``IMoveDefaultsStore``.

A transporter's defaults are read on every move and written rarely, both on the
event loop, so this is one ``asyncio.Lock`` over the store and nothing else. The
Service itself satisfies ``IMoveDefaultsStore``, so a facade and the transporter
consume it as a drop-in store.

``apply`` holds the lock across the read and the write, because an edit that
sets one field has to merge into whatever the row already holds; two edits
racing on separate reads would each write a patch missing the other's field.
"""

import asyncio
from collections.abc import Iterable

from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch

from orca.runtime.interfaces import IMoveDefaultsStore


class MoveDefaultsService:
    """Orchestrates move-defaults reads and writes over a per-DB store."""

    def __init__(self, store: IMoveDefaultsStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def get(self, transporter_name: str) -> MoveParameterPatch | None:
        async with self._lock:
            return await self._store.get(transporter_name)

    async def list(self) -> dict[str, MoveParameterPatch]:
        async with self._lock:
            return await self._store.list()

    async def set(
        self, transporter_name: str, patch: MoveParameterPatch,
    ) -> None:
        async with self._lock:
            await self._store.set(transporter_name, patch)

    async def apply(
        self,
        transporter_name: str,
        patch: MoveParameterPatch,
        clear: Iterable[MoveParameterField] = (),
    ) -> MoveParameterPatch:
        """Merge an edit into the stored row and return what the row now holds.

        Setting and clearing in one call is deliberate: an operator swapping a
        gripper changes some numbers and hands others back to the seed, and
        splitting that into two writes leaves the arm briefly on a mixture
        neither call intended.
        """
        async with self._lock:
            stored = await self._store.get(transporter_name) or MoveParameterPatch()
            merged = patch.over(stored).without(clear)
            if merged.model_dump(exclude_none=True):
                await self._store.set(transporter_name, merged)
            else:
                await self._store.delete(transporter_name)
            return merged

    async def delete(self, transporter_name: str) -> bool:
        async with self._lock:
            return await self._store.delete(transporter_name)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies IMoveDefaultsStore)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()
