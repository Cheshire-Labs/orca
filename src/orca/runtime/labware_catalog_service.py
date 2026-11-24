"""LabwareCatalogService: the single source of truth for the labware catalog.

Everything routes through this one Service: the operator CRUD surface
(``deployment_registries.labware_catalog``), the runtime's resolution store
(``rt.labware_catalog_store``), and the build-time read view
(``StoreBackedLabwareCatalog``). Policy (force operator_custom, refuse seed
mutation, conflict, geometry validation) lives here; storage lives behind the
injected ``ILabwareCatalogStore``. Every method serializes on one
``asyncio.Lock`` so concurrent mutations and reads do not interleave.

Errors are orca-domain exceptions the REST layers map to HTTP:
``LabwareNotFound`` -> 404, ``LabwareCatalogConflict`` -> 409,
``SeedLabwareReadOnly`` -> 409, ``LabwareGeometryInvalid`` -> 422.
"""

import asyncio
from typing import List, Protocol

from cheshire_drivers.labware_seed import LabwareSeedEntry
from pydantic import TypeAdapter, ValidationError

from orca.runtime.facades.labware_catalog import ILabwareCatalogFacade
from orca.resource_models.labware_catalog_protocol import LabwareNotFound
from orca.runtime.setup_files import is_plain_file_name
from orca.runtime.labware_catalog_store import (
    OPERATOR_CUSTOM_SOURCE,
    SEED_SOURCE,
    ILabwareCatalogStore,
    LabwareCatalogEntry,
)


_ENTRY_ADAPTER: TypeAdapter[LabwareSeedEntry] = TypeAdapter(LabwareSeedEntry)


class LabwareCatalogConflict(Exception):
    """A catalog row with the same labware_type already exists."""


class SeedLabwareReadOnly(Exception):
    """Refused mutation of a PLR-seed row; only operator_custom is mutable."""


class LabwareGeometryInvalid(Exception):
    """The entry's geometry does not validate to a LabwareSeedEntry."""


class ILabwareCatalogService(ILabwareCatalogFacade, Protocol):
    """The labware catalog: the bundled definitions plus the ones an operator added."""

    async def contains(self, labware_type: str) -> bool: ...


class LabwareCatalogService(ILabwareCatalogFacade):
    """Orchestrates catalog CRUD + policy over a per-DB store under one lock."""

    def __init__(self, store: ILabwareCatalogStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def list(
        self, category: str | None = None,
    ) -> List[LabwareCatalogEntry]:
        async with self._lock:
            return await self._store.list(category)

    async def get(self, labware_type: str) -> LabwareCatalogEntry:
        async with self._lock:
            entry = await self._store.get(labware_type)
        if entry is None:
            raise LabwareNotFound(
                f"labware_type {labware_type!r} not in catalog",
            )
        return entry

    async def contains(self, labware_type: str) -> bool:
        async with self._lock:
            return await self._store.get(labware_type) is not None

    async def add(self, entry: LabwareCatalogEntry) -> LabwareCatalogEntry:
        entry = entry.model_copy(update={"source": OPERATOR_CUSTOM_SOURCE})
        async with self._lock:
            if await self._store.get(entry.labware_type) is not None:
                raise LabwareCatalogConflict(
                    f"labware_type {entry.labware_type!r} already exists in catalog",
                )
            self._validate_geometry(entry)
            await self._store.put(entry)
        return entry

    async def update(self, entry: LabwareCatalogEntry) -> LabwareCatalogEntry:
        async with self._lock:
            existing = await self._store.get(entry.labware_type)
            if existing is None:
                raise LabwareNotFound(
                    f"labware_type {entry.labware_type!r} not in catalog",
                )
            self._refuse_seed(existing)
            entry = entry.model_copy(update={"source": OPERATOR_CUSTOM_SOURCE})
            self._validate_geometry(entry)
            await self._store.put(entry)
        return entry

    async def delete(self, labware_type: str) -> None:
        async with self._lock:
            existing = await self._store.get(labware_type)
            if existing is None:
                raise LabwareNotFound(
                    f"labware_type {labware_type!r} not in catalog",
                )
            self._refuse_seed(existing)
            await self._store.delete(labware_type)

    async def create_schema(self) -> None:
        """Create the store's schema if absent (satisfies the store contract)."""
        await self._store.create_schema()

    async def ensure_schema(self) -> None:
        """Runtime-lifecycle alias for ``create_schema`` (in-memory/sim setup)."""
        await self._store.create_schema()

    async def aclose(self) -> None:
        """Release the store (dispose its engine). Call from the owner's shutdown."""
        await self._store.aclose()

    @staticmethod
    def _refuse_seed(entry: LabwareCatalogEntry) -> None:
        if entry.source == SEED_SOURCE:
            raise SeedLabwareReadOnly(
                f"labware_type {entry.labware_type!r} is a read-only PLR-seed "
                "row; only operator_custom rows are mutable",
            )

    @staticmethod
    def _validate_geometry(entry: LabwareCatalogEntry) -> None:
        """Geometry must validate to a LabwareSeedEntry AND its identity/
        category must match the row's denormalized columns.

        The build-time ``ILabwareCatalog`` snapshot rebuilds each entry purely
        from ``geometry``, so a row whose ``labware_type`` / ``category`` columns
        disagree with its geometry would resolve under a different key at build
        time (and vanish from workflow resolution). Enforcing equality is what
        makes the denormalization safe; it also pins ``category`` to the
        seed-union's valid set on both backends.
        """
        if not is_plain_file_name(entry.labware_type):
            raise LabwareGeometryInvalid(
                f"labware_type {entry.labware_type!r} cannot be a file name",
            )
        try:
            validated = _ENTRY_ADAPTER.validate_python(entry.geometry)
        except ValidationError as exc:
            raise LabwareGeometryInvalid(
                f"geometry for labware_type {entry.labware_type!r} is not a "
                f"valid labware definition: {exc}",
            ) from exc
        if validated.labware_type != entry.labware_type:
            raise LabwareGeometryInvalid(
                f"geometry labware_type {validated.labware_type!r} does not "
                f"match the entry labware_type {entry.labware_type!r}",
            )
        if validated.category != entry.category:
            raise LabwareGeometryInvalid(
                f"geometry category {validated.category!r} does not match the "
                f"entry category {entry.category!r}",
            )
