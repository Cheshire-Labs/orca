"""DB-neutral mapping between ``LabwareCatalogEntry`` and ``LabwareCatalogRow``.

Lives in orca-core; the per-DB catalog stores reuse it. The geometry is
persisted as one JSON blob and the denormalized facet columns are written
inline so the list surface round-trips without re-parsing the blob.
"""

from orca.runtime.db.models import LabwareCatalogRow
from orca.runtime.labware_catalog_store import LabwareCatalogEntry


def catalog_entry_to_row(entry: LabwareCatalogEntry) -> LabwareCatalogRow:
    return LabwareCatalogRow(
        labware_type=entry.labware_type,
        display_name=entry.display_name,
        category=entry.category,
        vendor=entry.vendor,
        source=entry.source,
        geometry=entry.geometry,
        plr_class_name=entry.plr_class_name,
    )


def apply_catalog_entry_to_row(
    row: LabwareCatalogRow, entry: LabwareCatalogEntry,
) -> None:
    """Overwrite a row's columns from an entry (upsert path; PK fixed)."""
    row.display_name = entry.display_name
    row.category = entry.category
    row.vendor = entry.vendor
    row.source = entry.source
    row.geometry = entry.geometry
    row.plr_class_name = entry.plr_class_name


def row_to_catalog_entry(row: LabwareCatalogRow) -> LabwareCatalogEntry:
    return LabwareCatalogEntry(
        labware_type=row.labware_type,
        display_name=row.display_name,
        category=row.category,
        vendor=row.vendor,
        source=row.source,
        geometry=row.geometry,
        plr_class_name=row.plr_class_name,
    )
