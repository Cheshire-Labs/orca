"""ILabwareCatalog implementations.

``StoreBackedLabwareCatalog`` is the production read path: it reads through the
``LabwareCatalogService`` (the single source of truth) on every query,
with no in-memory snapshot in front of the DB. ``InMemoryLabwareCatalog``
is a direct dict-backed catalog used by tests and sim code that construct a
catalog from a fixed set of definitions without a store.
"""

from typing import Iterable

from pydantic import TypeAdapter

from orca.runtime.labware_catalog_protocol import (
    ILabwareCatalog,
    LabwareDefinition,
    LabwareNotFound,
)
from orca.runtime.labware_catalog_service import LabwareCatalogService


class InMemoryLabwareCatalog(ILabwareCatalog):
    """In-process labware catalog backed by a single dict.

    Insertion order is preserved (dict semantics on 3.7+) so `list()` is
    deterministic for callers that depend on stable iteration.
    """

    def __init__(self, entries: Iterable[LabwareDefinition] = ()) -> None:
        self._by_labware_type: dict[str, LabwareDefinition] = {e.labware_type: e for e in entries}

    async def get(self, labware_type: str) -> LabwareDefinition:
        try:
            return self._by_labware_type[labware_type]
        except KeyError as exc:
            raise LabwareNotFound(
                f"labware labware_type {labware_type!r} not in catalog "
                f"(available: {len(self._by_labware_type)} entries)"
            ) from exc

    async def list(self, category: str | None = None) -> list[LabwareDefinition]:
        if category is None:
            return list(self._by_labware_type.values())
        return [e for e in self._by_labware_type.values() if e.category == category]

    async def contains(self, labware_type: str) -> bool:
        return labware_type in self._by_labware_type


class StoreBackedLabwareCatalog(ILabwareCatalog):
    """ILabwareCatalog that reads through the catalog Service per query.

    Each stored row carries its definition as a ``geometry`` JSON blob (a
    ``LabwareSeedEntry`` dump); this projects it back to the typed entry. The
    Service is the single source of truth, so build-time resolution and the
    operator CRUD surface see the same rows under the same lock.
    """

    def __init__(self, service: LabwareCatalogService) -> None:
        self._service = service
        self._adapter: TypeAdapter[LabwareDefinition] = TypeAdapter(LabwareDefinition)

    async def get(self, labware_type: str) -> LabwareDefinition:
        entry = await self._service.get(labware_type)
        return self._adapter.validate_python(entry.geometry)

    async def list(self, category: str | None = None) -> list[LabwareDefinition]:
        rows = await self._service.list(category)
        return [self._adapter.validate_python(r.geometry) for r in rows]

    async def contains(self, labware_type: str) -> bool:
        return await self._service.contains(labware_type)
