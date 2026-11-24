"""A plate hotel is a passive rack: each shelf is a plate pad an arm reaches at `<hotel>/shelf_<n>`."""

from orca.events.event_bus import EventBus
import pytest

from cheshire_drivers import CartesianCoordinates, Teachpoint
from orca.resource_models.plate_hotel import PlateHotel
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.package import build_system
from tests.memory_activity import memory_ops_store
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints


async def _build() -> SystemBuild:
    stores = package_stores()
    c = CartesianCoordinates
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("pad", c(200, 200, 40, 0, 90, 180), orientation="right"),
        *(Teachpoint(f"hotel/shelf_{n}", c(400, 0, 40 * n, 0, 90, 180), orientation="right") for n in (1, 2, 3)),
    ]))
    topology = Topology(locations={"hotel": PlateHotel("hotel", shelves=3), "pad": PlatePad("pad")}, transporters=[arm])
    return await build_system("plate-hotel", topology, stores, event_bus=EventBus(), ops_history_store=memory_ops_store())


async def test_each_shelf_is_a_location_of_its_own_and_the_hotel_is_not() -> None:
    system_map = (await _build()).system.system_map
    names = {loc.position_id for loc in system_map.locations}

    assert {"hotel/shelf_1", "hotel/shelf_2", "hotel/shelf_3", "pad"} <= names
    assert "hotel" not in names
    assert isinstance(system_map.get_location("hotel/shelf_2").resource, PlatePad)


async def test_the_hotel_is_a_resource_and_the_arm_reaches_every_shelf() -> None:
    build = await _build()
    system_map = build.system.system_map

    assert isinstance(build.system.get_resource("hotel"), PlateHotel)
    for shelf in ("hotel/shelf_1", "hotel/shelf_2", "hotel/shelf_3"):
        assert system_map.has_any_route("pad", shelf), shelf


def test_a_hotel_needs_a_shelf() -> None:
    with pytest.raises(ValueError, match="at least one shelf"):
        PlateHotel("hotel", shelves=0)



def test_each_shelf_is_named_on_the_hotels_model_apart_from_its_location() -> None:
    hotel = PlateHotel("hotel_1", shelves=3)

    assert [(pad.name, hotel.site_name(pad)) for pad in hotel.sites] == [
        ("hotel_1/shelf_1", "shelf_1"), ("hotel_1/shelf_2", "shelf_2"), ("hotel_1/shelf_3", "shelf_3"),
    ]
