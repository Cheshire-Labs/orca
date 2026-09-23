"""Labware-catalog store: pure CRUD over catalog rows.

Distinct from ``ILabwareCatalog`` (the async, read-only view used for
workflow ``@orca.thread(labware=...)`` resolution). The store is the async,
mutable substrate the ``LabwareCatalogService`` orchestrates; everything
(operator CRUD, build-time reads) routes through that Service.

Pure storage CRUD: policy (force operator_custom, refuse seed mutation,
conflict, geometry validation) lives in ``LabwareCatalogService``. Impls:
``SqliteLabwareCatalogStore`` (source-available persistence) and a hosted deployment's DB-backed store.
"""

from typing import Protocol

from cheshire_drivers.labware_seed import LabwareSeedEntry
from pydantic import BaseModel, ConfigDict, JsonValue


SEED_SOURCE = "plr_seed"
OPERATOR_CUSTOM_SOURCE = "operator_custom"


class LabwareCatalogSummary(BaseModel):
    """One catalog row WITHOUT the geometry blob: identity + facets only.

    The list surface returns these; geometry is fetched per row via ``get``.
    A 384-well plate's geometry is ~77 KB, so a full-catalog list of geometry
    blobs runs to megabytes -- past the MCP 1 MB result cap. Discovery needs
    identity only.
    """

    model_config = ConfigDict(frozen=True)

    labware_type: str
    display_name: str
    category: str
    vendor: str | None = None
    source: str
    plr_class_name: str | None = None


class LabwareCatalogEntry(BaseModel):
    """One catalog row: identity + denormalized facets + the full geometry blob.

    ``geometry`` is the ``LabwareSeedEntry`` dump (the seeder writes
    ``entry.model_dump(mode="json")`` and the build-time catalog reads it
    back), so every row -- seed or operator-custom -- round-trips to a typed
    seed entry. ``source`` is ``plr_seed`` (read-only) or ``operator_custom``.
    """

    model_config = ConfigDict(frozen=True)

    labware_type: str
    display_name: str
    category: str
    vendor: str | None = None
    source: str
    geometry: dict[str, JsonValue]
    plr_class_name: str | None = None

    def to_summary(self) -> LabwareCatalogSummary:
        return LabwareCatalogSummary(
            labware_type=self.labware_type,
            display_name=self.display_name,
            category=self.category,
            vendor=self.vendor,
            source=self.source,
            plr_class_name=self.plr_class_name,
        )


def seed_entry_to_catalog_entry(seed: LabwareSeedEntry) -> LabwareCatalogEntry:
    """Project a PLR seed entry into a catalog row (source='plr_seed')."""
    return LabwareCatalogEntry(
        labware_type=seed.labware_type,
        display_name=seed.display_name,
        category=seed.category,
        vendor=seed.vendor,
        source=SEED_SOURCE,
        geometry=seed.model_dump(mode="json"),
        plr_class_name=seed.plr_class_name,
    )


class ILabwareCatalogStore(Protocol):
    """Async CRUD over catalog rows. Swap SQLite / Postgres.

    Pure storage: no policy. ``put`` is an upsert; ``delete`` is idempotent.
    The ``LabwareCatalogService`` enforces read-only-seed, conflict, and
    source rules.
    """

    async def list(self, category: str | None = None) -> list[LabwareCatalogEntry]: ...

    async def get(self, labware_type: str) -> LabwareCatalogEntry | None: ...

    async def put(self, entry: LabwareCatalogEntry) -> None: ...

    async def delete(self, labware_type: str) -> None: ...

    async def create_schema(self) -> None: ...

    async def aclose(self) -> None: ...
