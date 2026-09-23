"""Cutover C1: build_system produces flat site nodes.

Target structure pinned here:
- Every device-owned labware position is a FLAT DeckSiteLocation routing node
  named "<device>/<site>"; a single-slot device gets "<device>/slot".
- The device name itself is NOT a routing node (it is the reservation mutex
  key only); start=/end= naming a single-slot device resolves to its slot.
- Handoff-ness is DERIVED topology: the arm reaches exactly the deck sites
  it teaches (site-qualified points); the internal gripper relays between
  all deck sites. Multi-site device names are invalid in journeys.
"""
import pytest

from cheshire_drivers import CartesianCoordinates, DeckLayoutConfig, Teachpoint
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild, Topology, build_system

FLEX_DECK = DeckLayoutConfig(deck_type="FlexDeck", resources=[])
_DECK_HANDOFF = "D3-slot"
_PLATE_SLOT = "C2-slot"


async def _build() -> SystemBuild:
    stores = InMemoryRuntimeStoreFactory()
    flex = LiquidHandler(
        "flex",
        deck_layout_store=stores.deck_layouts("flex", seed={"default": FLEX_DECK}),
        deck_layout="default",
        sim=True,
    )
    stacker = Storage("stacker")
    waste = Storage("waste")
    pad = PlatePad("pad")
    c = CartesianCoordinates
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint("stacker", c(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("pad", c(200, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint(f"flex/{_DECK_HANDOFF}", c(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", c(600, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )
    topology = Topology(
        locations={"flex": flex, "stacker": stacker, "waste": waste, "pad": pad},
        transporters=[arm],
    )
    return await build_system("flat-site-build", topology, stores)


async def test_deck_sites_are_flat_routing_nodes_with_owners() -> None:
    build = await _build()
    system_map = build.system.system_map
    node_ids = {loc.position_id for loc in system_map.locations}

    assert f"flex/{_PLATE_SLOT}" in node_ids
    site = system_map.get_location(f"flex/{_PLATE_SLOT}")
    assert isinstance(site, DeckSiteLocation)
    assert site.owner.name == "flex"


async def test_device_name_is_not_a_routing_node() -> None:
    build = await _build()
    node_ids = {loc.position_id for loc in build.system.system_map.locations}
    assert "flex" not in node_ids
    assert "stacker" not in node_ids


async def test_single_slot_device_gets_a_flat_slot_site() -> None:
    build = await _build()
    system_map = build.system.system_map
    slot = system_map.get_location("stacker/slot")
    assert isinstance(slot, DeckSiteLocation)
    assert slot.owner.name == "stacker"
    #Device name -> single site for journeys; the bare
    # name stays the reservation-layer mutex Location, off the graph.
    assert system_map.resolve_journey_location("stacker") is slot
    mutex = system_map.get_location("stacker")
    assert mutex.position_id == "stacker"
    assert mutex is not slot


async def test_arm_teaches_sites_and_gripper_relays_everywhere() -> None:
    build = await _build()
    system_map = build.system.system_map
    # The arm reaches exactly the site it teaches; handoff-ness is derived.
    assert system_map.has_any_route("pad", f"flex/{_DECK_HANDOFF}")
    # The internal gripper relays from there to ANY other deck site.
    assert system_map.has_any_route("pad", f"flex/{_PLATE_SLOT}")
    transporter = system_map.get_transporter_between(
        f"flex/{_DECK_HANDOFF}", f"flex/{_PLATE_SLOT}"
    )
    assert transporter.name == "flex/gripper"
    # An UNQUALIFIED multi-site device name is a loud journey error.
    with pytest.raises(KeyError, match="multi-site device"):
        system_map.resolve_journey_location("flex")


async def test_deck_sites_are_never_deadlock_resolution_targets() -> None:
    build = await _build()
    paths = build.system.system_map.get_shortest_paths_to_deadlock_resolution(
        f"flex/{_PLATE_SLOT}"
    )
    park_targets = {path[-1] for path in paths}
    assert park_targets == {"pad"}
