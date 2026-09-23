"""ILabwareCatalogFacade: the operator-facing catalog CRUD contract.

The implementation is ``LabwareCatalogService`` (single source of truth over an
``ILabwareCatalogStore``); the deployment-registries layer exposes it as
``deployment_registries.labware_catalog`` so REST/MCP/CLI catalog operations on
BOTH backends go through one path. The policy exceptions
(``LabwareCatalogConflict``, ``SeedLabwareReadOnly``, ``LabwareGeometryInvalid``)
live in ``orca.runtime.labware_catalog_service`` alongside that implementation.
"""

from abc import ABC, abstractmethod

from orca.runtime.labware_catalog_store import LabwareCatalogEntry


class ILabwareCatalogFacade(ABC):
    """Operator-facing catalog CRUD. ``deployment_registries.labware_catalog``."""

    @abstractmethod
    async def list(
        self, category: str | None = None,
    ) -> list[LabwareCatalogEntry]: ...

    @abstractmethod
    async def get(self, labware_type: str) -> LabwareCatalogEntry: ...

    @abstractmethod
    async def add(self, entry: LabwareCatalogEntry) -> LabwareCatalogEntry: ...

    @abstractmethod
    async def update(self, entry: LabwareCatalogEntry) -> LabwareCatalogEntry: ...

    @abstractmethod
    async def delete(self, labware_type: str) -> None: ...
