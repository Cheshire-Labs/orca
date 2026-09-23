"""A device's deck site is an admissible INTERIOR hop only
when the route starts or ends on that device -- never a corridor between two
foreign endpoints.

Flat sites are real routing nodes, so without the ban `nx.all_shortest_paths`
happily threads a route THROUGH an unrelated device's deck (a plate crossing an
idle liquid handler as a shortcut). The service set is derived from the path
itself: {owner of path[0], owner of path[-1]} minus None -- which admits the
inbound chained handoff, the departure relay out of a multi-site deck, and the
parking exit hop, while rejecting every foreign corridor.
"""
from unittest.mock import Mock

import pytest

from cheshire_drivers import CartesianCoordinates, DeckLayoutConfig, Teachpoint
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild, Topology, build_system
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.system_map import SystemMap

FLEX_DECK = DeckLayoutConfig(deck_type="FlexDeck", resources=[])
_HANDOFF = "D3-slot"
_PLATE_SLOT = "C2-slot"
_C = CartesianCoordinates


async def _build_flex_bench() -> SystemBuild:
    """One arm serving a flex deck, a stacker, a waste, and a parking pad."""
    stores = InMemoryRuntimeStoreFactory()
    flex = LiquidHandler(
        "flex",
        deck_layout_store=stores.deck_layouts("flex", seed={"default": FLEX_DECK}),
        deck_layout="default",
        sim=True,
    )
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint("stacker", _C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("pad", _C(200, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint(f"flex/{_HANDOFF}", _C(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", _C(600, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )
    topology = Topology(
        locations={
            "flex": flex,
            "stacker": Storage("stacker"),
            "waste": Storage("waste"),
            "pad": PlatePad("pad"),
        },
        transporters=[arm],
    )
    return await build_system("transit-ban-bench", topology, stores)


async def _build_two_arm_corridor() -> SystemBuild:
    """Two arms that only meet at the flex's arm-taught site.

    stacker (arm_a) -> flex/D3-slot <- (arm_b) waste, pad: every stacker->waste
    or stacker->pad route must corridor THROUGH the flex deck, which the ban
    forbids -- the flex is not a transfer station.
    """
    stores = InMemoryRuntimeStoreFactory()
    flex = LiquidHandler(
        "flex",
        deck_layout_store=stores.deck_layouts("flex", seed={"default": FLEX_DECK}),
        deck_layout="default",
        sim=True,
    )
    arm_a = Transporter(
        "arm_a",
        teachpoint_store=stores.teachpoints("arm_a", seed=[
            Teachpoint("stacker", _C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint(f"flex/{_HANDOFF}", _C(400, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )
    arm_b = Transporter(
        "arm_b",
        teachpoint_store=stores.teachpoints("arm_b", seed=[
            Teachpoint(f"flex/{_HANDOFF}", _C(400, 210, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", _C(600, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("pad", _C(800, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )
    topology = Topology(
        locations={
            "flex": flex,
            "stacker": Storage("stacker"),
            "waste": Storage("waste"),
            "pad": PlatePad("pad"),
        },
        transporters=[arm_a, arm_b],
    )
    return await build_system("transit-ban-corridor", topology, stores)


async def test_corridor_cut_through_a_foreign_deck_is_inadmissible() -> None:
    build = await _build_two_arm_corridor()
    system_map = build.system.system_map
    corridor = ["stacker/slot", f"flex/{_HANDOFF}", "waste/slot"]
    assert system_map.get_all_shortest_any_paths("stacker/slot", "waste/slot") == [corridor], (
        "fixture invariant: the ONLY stacker->waste route corridors through the flex"
    )
    assert not system_map.is_path_admissible(corridor), (
        "a route between two foreign endpoints may not transit the flex's deck"
    )


async def test_inbound_chained_handoff_is_admissible() -> None:
    build = await _build_flex_bench()
    system_map = build.system.system_map
    assert system_map.is_path_admissible(
        ["stacker/slot", f"flex/{_HANDOFF}", f"flex/{_PLATE_SLOT}"]
    ), "delivering INTO the flex legitimately relays through its own handoff site"


async def test_departure_relay_out_of_a_deck_is_admissible() -> None:
    """The review-critical case: a plate finishing on a non-arm-taught working
    slot must relay out through its OWN device's arm-taught site. A terminal-only
    service set would reject this and strand every departing plate."""
    build = await _build_flex_bench()
    system_map = build.system.system_map
    assert system_map.is_path_admissible(
        [f"flex/{_PLATE_SLOT}", f"flex/{_HANDOFF}", "waste/slot"]
    )


async def test_every_interior_node_is_checked_not_just_the_first() -> None:
    build = await _build_flex_bench()
    system_map = build.system.system_map
    # The foreign flex site is the SECOND interior hop; an endpoints-only or
    # first-interior-only predicate would wrongly admit this path.
    assert not system_map.is_path_admissible(
        ["stacker/slot", "pad", f"flex/{_HANDOFF}", "waste/slot"]
    )


async def test_resolver_applies_the_transit_ban() -> None:
    """The move resolver must FILTER inadmissible paths, so a corridor-only
    route pair has no route at all rather than a banned one."""
    build = await _build_two_arm_corridor()
    system_map = build.system.system_map
    starvation = Mock()
    starvation.get_starvation_score.return_value = 0
    handler = MoveHandler(Mock(), system_map, starvation)
    labware = Mock()
    labware.id = "plate-1"

    with pytest.raises(ValueError, match="No routes found"):
        await handler.resolve_move_action(
            "thread-a",
            labware,
            system_map.get_location("stacker/slot"),
            [system_map.get_location("waste/slot")],
        )


async def test_parking_from_a_deck_exits_via_its_own_site() -> None:
    """A deadlock-parked plate leaving the flex legitimately relays through the
    flex's own arm-taught site toward the pad."""
    build = await _build_two_arm_corridor()
    system_map = build.system.system_map
    paths = system_map.get_shortest_paths_to_deadlock_resolution(f"flex/{_PLATE_SLOT}")
    assert paths, "the departure relay must survive the ban"
    assert {path[-1] for path in paths} == {"pad"}


async def test_parking_never_transits_a_foreign_deck() -> None:
    build = await _build_two_arm_corridor()
    system_map = build.system.system_map
    # stacker's only pad route corridors through the flex, so with the ban it
    # has NO legal parking route: not a thoroughfare even in deadlock recovery.
    assert system_map.get_shortest_paths_to_deadlock_resolution("stacker/slot") == []


async def test_park_targets_exclude_platepad_resourced_device_sites() -> None:
    """Park eligibility is owner-based, not just resource-typed: a device site
    constructed with a PlatePad resource still must never be a park target."""
    registry = Mock()
    registry.transporters = []
    registry.movers = []
    system_map = SystemMap(registry)
    source = PlatePad("src")
    await system_map.add_location(
        DeckSiteLocation("dev/x", owner=Storage("dev"), resource=PlatePad("x"))
    )
    from orca.resource_models.location import Location

    await system_map.add_location(Location("src", source))
    await system_map.add_location(Location("legal-pad", PlatePad("legal-pad")))
    mover = Mock()
    mover.labware = None
    await system_map.add_edge("src", "dev/x", mover)
    await system_map.add_edge("src", "legal-pad", mover)

    paths = system_map.get_shortest_paths_to_deadlock_resolution("src")
    assert {path[-1] for path in paths} == {"legal-pad"}


async def test_available_graph_preserves_edge_weights() -> None:
    build = await _build_flex_bench()
    system_map = build.system.system_map
    source, target, _ = system_map._graph.get_all_edges()[0]
    system_map.set_edge_weight(source, target, 7.5)

    available = system_map._get_available_graph()
    assert available.get_edge_data(source, target)["weight"] == 7.5, (
        "the availability rebuild must carry real edge weights, not flatten to 1.0"
    )
