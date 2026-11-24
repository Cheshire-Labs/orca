"""PackageSetupStoreFactory holds one catalog Service. The read-only
``labware_catalog()`` view reads through it per query, so added labware resolves
in workflows on the next read.
"""

from orca.runtime.labware_catalog_store import LabwareCatalogEntry
from tests.setup_package import package_stores


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


async def test_the_factory_catalog_holds_the_built_ins() -> None:
    catalog = package_stores().labware_catalog()
    assert len(await catalog.list()) > 0


async def test_build_catalog_includes_operator_custom_rows() -> None:
    # Single-source: a custom row added through the shared Service must resolve
    # in the build-time catalog, not just on the operator surface.
    factory = package_stores()
    await factory.labware_catalog_store().add(_custom_plate("my_custom_plate"))

    catalog = factory.labware_catalog()
    assert await catalog.contains("my_custom_plate")
    # the custom row carries through as its typed seed entry
    assert (await catalog.get("my_custom_plate")).labware_type == "my_custom_plate"
