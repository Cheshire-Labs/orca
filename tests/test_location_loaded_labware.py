"""loaded_labware on a plain Location under the flat model.

Relocated from the deleted test_parent_child_locations.py: with the parent/child
tree gone, a location aggregates only its own occupant, no child recursion.
"""
import pytest

from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from tests.test_helpers import create_test_labware_instance


@pytest.mark.asyncio
async def test_location_loaded_labware_is_its_own_occupant() -> None:
    loc = Location("shaker1", PlatePad("shaker1"))
    lw = await create_test_labware_instance("plate_1")
    loc.resource.initialize_labware(lw)

    assert loc.loaded_labware == [lw]


@pytest.mark.asyncio
async def test_empty_location_loaded_labware_is_empty() -> None:
    loc = Location("handoff", PlatePad("handoff"))
    assert loc.loaded_labware == []
