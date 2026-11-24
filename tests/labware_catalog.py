"""The bundled labware definitions as an in-memory catalog, for a system a test builds."""

from cheshire_drivers.labware_seed import load_labware_seed

from orca.resource_models.labware_catalog_protocol import ILabwareCatalog
from orca.runtime.labware_catalog import InMemoryLabwareCatalog


def bundled_labware_catalog() -> ILabwareCatalog:
    return InMemoryLabwareCatalog(load_labware_seed())
