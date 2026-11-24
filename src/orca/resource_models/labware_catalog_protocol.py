"""The labware catalog protocol, the definition alias and the lookup error."""

from typing import Protocol

from cheshire_drivers.labware_seed import LabwareSeedEntry


LabwareDefinition = LabwareSeedEntry
"""Catalog entry shape. Re-uses the discriminated-union shape from
cheshire_drivers.labware_seed so PLR seed entries and operator-custom
entries share one Pydantic-validated type. Plate entries satisfy
cheshire_drivers.plr.labware_converter._HasPlateGeometry; orca-client
reconstructs PLR Plates via that contract."""


class LabwareNotFound(KeyError):
    """Raised when a workflow references a labware_type not present in the catalog.

    Subclasses KeyError so callers that handle generic dict-style lookups
    still catch it, while specific handlers can distinguish via isinstance.
    """


class ILabwareCatalog(Protocol):
    """Read-only catalog of labware definitions, read from the DB per query.

    The catalog reads its backing store (SQLite, hosted or in-memory)
    on every lookup; no in-memory snapshot sits in front of it. A per-spawn read
    is negligible against physical lab timing. Operator edits to the underlying
    source surface immediately on the next read.

    Distinct from `ILabwareService`: this is the definition registry ("a
    Cor_96_wellplate has THIS geometry"); `ILabwareService` is the instance
    registry ("plate_abc is at shaker_1"). They do not overlap.

    The submission service resolves `@orca.thread(labware=<labware_type>)` references
    against `get(labware_type)` at registration time; an unknown labware_type fails the
    submission with `LabwareNotFound` rather than crashing the workflow mid-
    execution.
    """

    async def get(self, labware_type: str) -> LabwareDefinition: ...
    async def list(self, category: str | None = None) -> list[LabwareDefinition]: ...
    async def contains(self, labware_type: str) -> bool: ...
