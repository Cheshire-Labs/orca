"""LabwareCatalogService: the single CRUD surface + policy over the store.

Policy: add forces source='operator_custom' and rejects duplicates; update/
delete refuse PLR-seed rows (read-only) and 404 on missing; geometry must
validate to a LabwareSeedEntry (fail-fast vs a hosted deployment's silent build-time drop).
``seed_if_missing`` writes raw (source-preserving) and is idempotent; every
method serializes on one lock.
"""

import asyncio

import pytest

from orca.runtime.db import create_memory_engine
from orca.runtime.labware_catalog_protocol import LabwareNotFound
from orca.runtime.labware_catalog_service import (
    LabwareCatalogConflict,
    LabwareCatalogService,
    LabwareGeometryInvalid,
    SeedLabwareReadOnly,
)
from orca.runtime.labware_catalog_store import LabwareCatalogEntry
from orca.runtime.sqlite_labware_catalog_store import SqliteLabwareCatalogStore


# A minimal valid plate seed-entry dump usable as geometry.
def _plate_geometry(labware_type: str) -> dict:
    return {
        "category": "plate",
        "labware_type": labware_type,
        "display_name": labware_type,
        "vendor": None,
        "plr_class_name": None,
        "num_rows": 1,
        "num_cols": 1,
        "size_x": 1.0,
        "size_y": 1.0,
        "size_z": 1.0,
        "wells": [],
    }


def _entry(labware_type: str, source: str = "operator_custom") -> LabwareCatalogEntry:
    return LabwareCatalogEntry(
        labware_type=labware_type,
        display_name=labware_type.title(),
        category="plate",
        vendor=None,
        source=source,
        geometry=_plate_geometry(labware_type),
        plr_class_name=None,
    )


def _service() -> LabwareCatalogService:
    return LabwareCatalogService(SqliteLabwareCatalogStore(create_memory_engine()))


async def test_add_forces_operator_custom_source() -> None:
    service = _service()
    # Caller claims plr_seed; service overrides to operator_custom.
    added = await service.add(_entry("c", source="plr_seed"))
    assert added.source == "operator_custom"
    assert (await service.get("c")).source == "operator_custom"


async def test_add_duplicate_raises_conflict() -> None:
    service = _service()
    await service.add(_entry("c"))
    with pytest.raises(LabwareCatalogConflict):
        await service.add(_entry("c"))


async def test_add_invalid_geometry_raises() -> None:
    service = _service()
    bad = _entry("c").model_copy(update={"geometry": {"category": "plate"}})
    with pytest.raises(LabwareGeometryInvalid):
        await service.add(bad)


async def test_add_geometry_labware_type_mismatch_raises() -> None:
    # geometry describes "other"; the row claims "c". The build-time snapshot
    # would key this under "other", so the row must be rejected.
    service = _service()
    bad = _entry("c").model_copy(
        update={"geometry": _plate_geometry("other")},
    )
    with pytest.raises(LabwareGeometryInvalid):
        await service.add(bad)


async def test_add_geometry_category_mismatch_raises() -> None:
    # entry.category='plate' but the geometry is a valid tip_rack -> reject
    # (otherwise the denormalized category column lies about the geometry).
    service = _service()
    tip_rack_geom = {
        "category": "tip_rack",
        "labware_type": "c",
        "display_name": "c",
        "vendor": None,
        "plr_class_name": None,
        "num_rows": 1,
        "num_cols": 1,
        "size_x": 1.0,
        "size_y": 1.0,
        "size_z": 1.0,
        "tip_spots": [],
    }
    bad = _entry("c").model_copy(update={"geometry": tip_rack_geom})
    with pytest.raises(LabwareGeometryInvalid):
        await service.add(bad)


async def test_get_missing_raises_not_found() -> None:
    with pytest.raises(LabwareNotFound):
        await _service().get("missing")


async def test_update_seed_row_is_read_only() -> None:
    service = _service()
    await service.seed_if_missing([_entry("seeded", source="plr_seed")])
    with pytest.raises(SeedLabwareReadOnly):
        await service.update(_entry("seeded"))


async def test_update_missing_raises_not_found() -> None:
    with pytest.raises(LabwareNotFound):
        await _service().update(_entry("nope"))


async def test_update_custom_row_succeeds() -> None:
    service = _service()
    await service.add(_entry("c"))
    updated = await service.update(
        _entry("c").model_copy(update={"display_name": "Renamed"}),
    )
    assert updated.display_name == "Renamed"


async def test_delete_seed_row_is_read_only() -> None:
    service = _service()
    await service.seed_if_missing([_entry("seeded", source="plr_seed")])
    with pytest.raises(SeedLabwareReadOnly):
        await service.delete("seeded")


async def test_delete_custom_row_succeeds() -> None:
    service = _service()
    await service.add(_entry("c"))
    await service.delete("c")
    with pytest.raises(LabwareNotFound):
        await service.get("c")


async def test_contains_reflects_membership() -> None:
    service = _service()
    await service.add(_entry("c"))
    assert await service.contains("c")
    assert not await service.contains("missing")


async def test_seed_if_missing_preserves_source_and_is_idempotent() -> None:
    # seed_if_missing writes raw, so the plr_seed source survives (unlike add,
    # which forces operator_custom). Re-seeding never overwrites existing rows.
    service = _service()
    await service.seed_if_missing([_entry("s", source="plr_seed")])
    assert (await service.get("s")).source == "plr_seed"
    await service.seed_if_missing(
        [_entry("s", source="plr_seed").model_copy(update={"display_name": "X"})],
    )
    # Existing row wins; the re-declared seed body is ignored.
    assert (await service.get("s")).display_name == "S"


async def test_concurrent_distinct_adds_both_land() -> None:
    # Concurrent adds of distinct rows both persist (smoke: no corruption).
    service = _service()
    await asyncio.gather(service.add(_entry("a")), service.add(_entry("b")))
    rows = await service.list()
    assert {r.labware_type for r in rows} == {"a", "b"}


async def test_concurrent_same_key_adds_one_conflicts_under_the_lock() -> None:
    # The lock serializes add's get-check-then-put, so two concurrent adds of
    # the same key give exactly one success + one conflict, never two writes.
    service = _service()
    results = await asyncio.gather(
        service.add(_entry("dup")),
        service.add(_entry("dup")),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, LabwareCatalogConflict)]
    successes = [r for r in results if isinstance(r, LabwareCatalogEntry)]
    assert len(conflicts) == 1
    assert len(successes) == 1
    assert len(await service.list()) == 1
