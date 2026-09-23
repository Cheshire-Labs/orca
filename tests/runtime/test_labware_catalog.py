"""ILabwareCatalog contract + InMemoryLabwareCatalog behaviour.

The DB-backed adapter is not in this repo and is tested where it lives.
"""

import pytest
from cheshire_drivers.labware_seed import (
    LabwareSeedEntry,
    PlateSeedEntry,
    TipRackSeedEntry,
    load_labware_seed,
)
from pydantic import TypeAdapter

from orca.runtime.db import create_memory_engine
from orca.runtime.interfaces import (
    ILabwareCatalog,
    LabwareDefinition,
    LabwareNotFound,
)
from orca.runtime.labware_catalog import (
    InMemoryLabwareCatalog,
    StoreBackedLabwareCatalog,
)
from orca.runtime.labware_catalog_service import LabwareCatalogService
from orca.runtime.labware_catalog_store import seed_entry_to_catalog_entry
from orca.runtime.sqlite_labware_catalog_store import SqliteLabwareCatalogStore


@pytest.fixture(scope="module")
def real_plate() -> PlateSeedEntry:
    """A real plate entry from the bundled seed (Cor_96_wellplate_360ul_Fb)."""
    entries = load_labware_seed()
    return next(e for e in entries if isinstance(e, PlateSeedEntry) and e.labware_type == "Cor_96_wellplate_360ul_Fb")


@pytest.fixture(scope="module")
def real_rack() -> TipRackSeedEntry:
    entries = load_labware_seed()
    return next(e for e in entries if isinstance(e, TipRackSeedEntry))


async def test_in_memory_catalog_returns_registered_entries(real_plate: PlateSeedEntry) -> None:
    """`get(labware_type)` returns the entry passed at construction."""
    catalog: ILabwareCatalog = InMemoryLabwareCatalog([real_plate])
    got = await catalog.get(real_plate.labware_type)
    assert got is real_plate


async def test_in_memory_catalog_list_filters_by_category(
    real_plate: PlateSeedEntry, real_rack: TipRackSeedEntry,
) -> None:
    """`list(category=...)` returns only entries matching that category."""
    catalog = InMemoryLabwareCatalog([real_plate, real_rack])
    plates = await catalog.list(category="plate")
    racks = await catalog.list(category="tip_rack")
    assert {e.labware_type for e in plates} == {real_plate.labware_type}
    assert {e.labware_type for e in racks} == {real_rack.labware_type}


async def test_in_memory_catalog_list_no_filter_returns_all(
    real_plate: PlateSeedEntry, real_rack: TipRackSeedEntry,
) -> None:
    catalog = InMemoryLabwareCatalog([real_plate, real_rack])
    everything = await catalog.list()
    assert {e.labware_type for e in everything} == {real_plate.labware_type, real_rack.labware_type}


async def test_in_memory_catalog_raises_on_unknown_labware_type(real_plate: PlateSeedEntry) -> None:
    """`get` raises `LabwareNotFound` (a `KeyError` subclass) for missing labware_types."""
    catalog = InMemoryLabwareCatalog([real_plate])
    with pytest.raises(LabwareNotFound, match="not in catalog"):
        await catalog.get("nonexistent_plate_xyz")


async def test_in_memory_catalog_contains(real_plate: PlateSeedEntry) -> None:
    """`contains(labware_type)` is the membership predicate without raising."""
    catalog = InMemoryLabwareCatalog([real_plate])
    assert await catalog.contains(real_plate.labware_type)
    assert not await catalog.contains("nonexistent_plate_xyz")


async def test_in_memory_catalog_satisfies_protocol(real_plate: PlateSeedEntry) -> None:
    """`InMemoryLabwareCatalog` is structurally typed as `ILabwareCatalog`.

    Closes the static-type contract: passing the in-memory impl where the
    protocol is expected must be accepted by pyright.
    """

    async def _take_catalog(c: ILabwareCatalog) -> str:
        return (await c.get(real_plate.labware_type)).labware_type

    catalog = InMemoryLabwareCatalog([real_plate])
    assert await _take_catalog(catalog) == real_plate.labware_type


def test_labware_not_found_is_keyerror_subclass() -> None:
    """`LabwareNotFound` must be a `KeyError` subclass so generic dict-style
    handlers still match. Specific handlers can distinguish via isinstance.
    """
    assert issubclass(LabwareNotFound, KeyError)


async def test_in_memory_catalog_empty_construction() -> None:
    """Catalog can be constructed with no entries (sim/test default)."""
    catalog = InMemoryLabwareCatalog()
    assert await catalog.list() == []
    assert not await catalog.contains("anything")
    with pytest.raises(LabwareNotFound):
        await catalog.get("anything")


async def test_labware_definition_alias_is_labware_seed_entry(real_plate: PlateSeedEntry) -> None:
    """`LabwareDefinition` re-exports `cheshire_drivers.labware_seed.LabwareSeedEntry`.

    Pins the contract so callers get the seed module's discriminated-union
    shape (Pydantic validation, frozen semantics) and a real catalog lookup
    resolves to a value that validates as that union.
    """
    assert LabwareDefinition is LabwareSeedEntry

    catalog: ILabwareCatalog = InMemoryLabwareCatalog([real_plate])
    got = await catalog.get(real_plate.labware_type)
    # The looked-up entry round-trips through the alias's discriminated union,
    # proving the alias is the seed shape and the lookup yields a valid member.
    validated = TypeAdapter(LabwareDefinition).validate_python(got.model_dump())
    assert validated == real_plate


def _store_backed_catalog() -> tuple[StoreBackedLabwareCatalog, LabwareCatalogService]:
    service = LabwareCatalogService(SqliteLabwareCatalogStore(create_memory_engine()))
    return StoreBackedLabwareCatalog(service), service


async def test_store_backed_catalog_reads_through_the_service(
    real_plate: PlateSeedEntry,
) -> None:
    """StoreBackedLabwareCatalog projects each row's geometry through the
    Service back to its typed LabwareDefinition (get/list/contains)."""
    catalog, service = _store_backed_catalog()
    await service.seed_if_missing([seed_entry_to_catalog_entry(real_plate)])

    got = await catalog.get(real_plate.labware_type)
    assert got.labware_type == real_plate.labware_type
    assert await catalog.contains(real_plate.labware_type)
    listed = await catalog.list()
    assert {e.labware_type for e in listed} == {real_plate.labware_type}


async def test_store_backed_catalog_raises_on_unknown() -> None:
    """A missing labware_type raises LabwareNotFound (the read view's contract,
    surfaced by the Service's get)."""
    catalog, _ = _store_backed_catalog()
    assert not await catalog.contains("nonexistent_plate_xyz")
    with pytest.raises(LabwareNotFound):
        await catalog.get("nonexistent_plate_xyz")
