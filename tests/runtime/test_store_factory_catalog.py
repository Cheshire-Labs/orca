"""InMemoryRuntimeStoreFactory catalog single-source.

The factory holds ONE catalog Service (injectable so the daemon shares it across
mount/unload + the runtime + the deployment layer). The read-only
``labware_catalog()`` view reads THROUGH that Service per query, with no
snapshot, so operator-added custom rows resolve in workflows on the next read.
"""

from orca.runtime.labware_catalog_service import (
    LabwareCatalogService,
    seeded_labware_catalog_service,
)
from orca.runtime.labware_catalog_store import LabwareCatalogEntry
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory


def _custom_plate(labware_type: str) -> LabwareCatalogEntry:
    geometry = {
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
    return LabwareCatalogEntry(
        labware_type=labware_type,
        display_name=labware_type,
        category="plate",
        vendor=None,
        source="operator_custom",
        geometry=geometry,
        plr_class_name=None,
    )


def test_factory_returns_the_injected_catalog_service() -> None:
    service = seeded_labware_catalog_service()
    factory = InMemoryRuntimeStoreFactory(catalog_service=service)
    assert factory.labware_catalog_store() is service


async def test_default_factory_seeds_its_own_catalog_service() -> None:
    factory = InMemoryRuntimeStoreFactory()
    catalog = factory.labware_catalog()
    assert len(await catalog.list()) > 0  # PLR seed loaded


async def test_build_catalog_includes_operator_custom_rows() -> None:
    # Single-source: a custom row added through the shared Service must resolve
    # in the build-time catalog, not just on the operator surface.
    service: LabwareCatalogService = seeded_labware_catalog_service()
    await service.add(_custom_plate("my_custom_plate"))
    factory = InMemoryRuntimeStoreFactory(catalog_service=service)

    catalog = factory.labware_catalog()
    assert await catalog.contains("my_custom_plate")
    # the custom row carries through as its typed seed entry
    assert (await catalog.get("my_custom_plate")).labware_type == "my_custom_plate"
