"""A `HumanTransfer` is sent the taught site of each pick and place, and refuses a site nobody taught for the person."""

import pytest

from cheshire_drivers import AccessConfig, CartesianCoordinates, SimTransporterDriver, Teachpoint
from cheshire_drivers.transporter_models import PickAtCoordsRequest, PlaceAtCoordsRequest

from orca.driver_management.drivers.human_transfer import HumanTransfer
from orca.resource_models.device_error import TeachpointNotFoundError
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.sim_labware import SimPlateTemplate
from tests.mock import outside_any_system
from tests.real_device_path import connected, the_real_device_path
from tests.setup_package import teachpoint_file, with_teachpoints

pytestmark = pytest.mark.asyncio

_BY_HAND = AccessConfig(name="by_hand", access_type="vertical")


class _Person(SimTransporterDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.picked: list[str] = []
        self.placed: list[str] = []

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        self.picked.append(request.teachpoint.position_id)

    async def place_at_coords(self, request: PlaceAtCoordsRequest) -> None:
        self.placed.append(request.teachpoint.position_id)


async def _person_taught(*sites: str) -> tuple[HumanTransfer, _Person]:
    store = teachpoint_file(access_configs=[_BY_HAND])
    for site in sites:
        coordinates = CartesianCoordinates(x=0.0, y=0.0, z=0.0, yaw=0.0, pitch=0.0, roll=0.0)
        await store.add(Teachpoint(site, coordinates, orientation="right", access=_BY_HAND))
    driver = _Person("person")
    the_real_device_path().simulate_with("person", driver)
    person = await connected(with_teachpoints(outside_any_system(HumanTransfer("person")), store))
    return person, driver


async def _pad_holding_a_plate(name: str) -> Location:
    pad = PlatePad(name)
    pad.initialize_labware(await SimPlateTemplate("plate").create_instance())
    return Location(name, pad)


async def test_each_pick_and_place_names_its_site() -> None:
    person, driver = await _person_taught("pad_2", "bench")
    source = await _pad_holding_a_plate("pad_2")

    await person.pick(source)
    plate = source.labware
    assert plate is not None
    # The placement chokepoint records the plate in the jaws, not the mover; this stands in for it.
    await source.dispose_labware(plate)
    await person.gripper_location.place_labware(plate)
    await person.place(Location("bench", PlatePad("bench")))

    assert (driver.picked, driver.placed) == (["pad_2"], ["bench"])


async def test_a_site_not_taught_for_the_person_is_refused_and_nothing_is_sent() -> None:
    person, driver = await _person_taught("pad_2", "bench")

    with pytest.raises(TeachpointNotFoundError, match="pad_1"):
        await person.pick(await _pad_holding_a_plate("pad_1"))

    assert driver.picked == []
