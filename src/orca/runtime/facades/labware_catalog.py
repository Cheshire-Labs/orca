"""ILabwareCatalogFacade: the operator-facing catalog CRUD contract.

The implementation is ``LabwareCatalogService`` (single source of truth over an
``ILabwareCatalogStore``); the deployment-registries layer exposes it as
``deployment_registries.labware_catalog`` so REST/CLI catalog operations on
BOTH backends go through one path. The policy exceptions
(``LabwareCatalogConflict``, ``SeedLabwareReadOnly``, ``LabwareGeometryInvalid``)
live in ``orca.runtime.labware_catalog_service`` alongside that implementation.
"""

from typing import Protocol

from orca.runtime.labware_catalog_store import LabwareCatalogEntry


class ILabwareCatalogFacade(Protocol):
    """Operator-facing catalog CRUD. ``deployment_registries.labware_catalog``."""

    async def list(
        self, category: str | None = None,
    ) -> list[LabwareCatalogEntry]: ...

    async def get(self, labware_type: str) -> LabwareCatalogEntry: ...

    async def add(self, entry: LabwareCatalogEntry) -> LabwareCatalogEntry: ...

    async def update(self, entry: LabwareCatalogEntry) -> LabwareCatalogEntry: ...

    async def delete(self, labware_type: str) -> None: ...
