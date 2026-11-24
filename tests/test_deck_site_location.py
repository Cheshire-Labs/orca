"""Flat device-owned site nodes.

Pins the dead-code capability that the cutover activates: a
DeckSiteLocation is a flat routing node with a device owner, backed by a
DeckSite resource so it can never become a deadlock-resolution park target.
"""
from orca.resource_models.deck_site import DeckSite
from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.location import Location
from orca.sdk.build import add_device_sites
from orca.resource_models.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from tests.test_helpers import create_test_device


class _Owner:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name


def test_deck_site_location_is_a_flat_location_with_an_owner() -> None:
    owner = _Owner("mlstar_1")
    site = DeckSiteLocation("carrier-7-0", owner, site_name="carrier-7-0")
    assert isinstance(site, Location)
    assert site.owner is owner


def test_deck_site_location_resource_defaults_to_deck_site() -> None:
    site = DeckSiteLocation("carrier-7-0", _Owner("mlstar_1"), site_name="carrier-7-0")
    assert isinstance(site.resource, DeckSite)
    assert site.resource.supports_deadlock_resolution is False


async def test_system_map_admits_a_flat_site_node() -> None:
    system_map = SystemMap(ResourceRegistry())
    site = DeckSiteLocation("carrier-7-0", _Owner("mlstar_1"), site_name="carrier-7-0")
    await system_map.add_site_location(site)
    assert system_map.location_exists("carrier-7-0")
    assert system_map.get_location("carrier-7-0") is site


async def test_flat_site_node_is_never_a_deadlock_resolution_target() -> None:
    system_map = SystemMap(ResourceRegistry())
    site = DeckSiteLocation("carrier-7-0", _Owner("mlstar_1"), site_name="carrier-7-0")
    pad = Location("pad_1")
    await system_map.add_site_location(site)
    await system_map.add_location(pad)
    assert system_map.get_shortest_paths_to_deadlock_resolution("pad_1") == []


async def test_a_device_site_knows_its_name_on_the_device_apart_from_its_position_id() -> None:
    system_map = SystemMap(ResourceRegistry())
    device = create_test_device("reader_1", site_names=["tray", "stacker"])
    await add_device_sites(system_map, "bay_3", device)
    sites = [s for s in device.sites if isinstance(s, DeckSiteLocation)]
    assert [(s.position_id, s.site_name, s.owner.name) for s in sites] == [
        ("bay_3/tray", "tray", "reader_1"),
        ("bay_3/stacker", "stacker", "reader_1"),
    ]

