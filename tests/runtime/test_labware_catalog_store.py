"""SqliteLabwareCatalogStore: the source-available persistence labware-catalog store.

The store is the mutable substrate behind the catalog Service (PLR seed +
operator-custom rows). Policy (read-only-seed, conflict, geometry) lives in the
Service; the store is pure CRUD with an upsert ``put`` and idempotent ``delete``.
"""

from orca.runtime.db import create_memory_engine, create_sqlite_engine
from orca.runtime.labware_catalog_store import LabwareCatalogEntry
from orca.runtime.sqlite_labware_catalog_store import SqliteLabwareCatalogStore


def _custom(labware_type: str, category: str = "plate") -> LabwareCatalogEntry:
    return LabwareCatalogEntry(
        labware_type=labware_type,
        display_name=labware_type.title(),
        category=category,
        vendor=None,
        source="operator_custom",
        geometry={"category": category, "labware_type": labware_type},
        plr_class_name=None,
    )


def _store() -> SqliteLabwareCatalogStore:
    return SqliteLabwareCatalogStore(create_memory_engine())


async def test_put_get_delete_custom_row() -> None:
    store = _store()
    await store.put(_custom("custom_x"))
    got = await store.get("custom_x")
    assert got is not None and got.display_name == "Custom_X"
    assert await store.get("missing") is None
    await store.delete("custom_x")
    assert await store.get("custom_x") is None


async def test_put_is_upsert() -> None:
    store = _store()
    await store.put(_custom("c"))
    await store.put(
        _custom("c").model_copy(update={"display_name": "Renamed"}),
    )
    got = await store.get("c")
    assert got is not None and got.display_name == "Renamed"
    assert len(await store.list()) == 1


async def test_list_filters_by_category() -> None:
    store = _store()
    await store.put(_custom("p1", category="plate"))
    await store.put(_custom("t1", category="tip_rack"))
    plates = await store.list(category="plate")
    assert [e.labware_type for e in plates] == ["p1"]


async def test_list_is_ordered_by_labware_type() -> None:
    store = _store()
    await store.put(_custom("zeta"))
    await store.put(_custom("alpha"))
    assert [e.labware_type for e in await store.list()] == ["alpha", "zeta"]


async def test_delete_is_idempotent() -> None:
    store = _store()
    await store.delete("never-existed")  # no raise


async def test_row_round_trips_every_field() -> None:
    store = _store()
    entry = LabwareCatalogEntry(
        labware_type="full",
        display_name="Full Row",
        category="plate",
        vendor="Corning",
        source="plr_seed",
        geometry={"category": "plate", "labware_type": "full"},
        plr_class_name="Cor_Plate",
    )
    await store.put(entry)
    got = await store.get("full")
    assert got == entry


async def test_rows_persist_across_reopen(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    store = SqliteLabwareCatalogStore(create_sqlite_engine(db_path))
    await store.put(_custom("durable"))
    await store.aclose()

    reopened = SqliteLabwareCatalogStore(create_sqlite_engine(db_path))
    got = await reopened.get("durable")
    assert got is not None and got.labware_type == "durable"
    await reopened.aclose()
