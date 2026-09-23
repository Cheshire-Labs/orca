"""A transit waypoint must survive the build.

A teachpoint can name an outer waypoint via `gateway`, and the transporter
walks that chain before reaching the destination. Those waypoints are poses
the arm passes through, not places labware is ever left, so they name no
registered location.

The route-graph builder used to expand every taught name into a routing node
and raise on anything that was not a location or a device. That made a working
gateway chain fail the NEXT build and take the whole deployment to 503. It went
unnoticed because the gateway tests stop at the store and the driver, and the
build tests never used a gateway, so the two halves were never exercised
together.
"""

import pytest

from cheshire_drivers import AccessConfig, CartesianCoordinates, Teachpoint
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild, Topology, build_system

_c = CartesianCoordinates
_ACCESS = AccessConfig(name="vert", access_type="vertical")


async def _build_with_waypoint() -> SystemBuild:
    """Two pads that both route through a shared transit pose."""
    stores = InMemoryRuntimeStoreFactory()
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint("safe_transit", _c(300, 0, 400, 0, 90, 180), orientation="right"),
            Teachpoint(
                "pad_1", _c(100, 200, 300, 0, 90, 180),
                orientation="right", access=_ACCESS, gateway="safe_transit",
            ),
            Teachpoint(
                "pad_2", _c(500, 200, 300, 0, 90, 180),
                orientation="right", access=_ACCESS, gateway="safe_transit",
            ),
        ]),
    )
    topology = Topology(
        locations={"pad_1": PlatePad("pad_1"), "pad_2": PlatePad("pad_2")},
        transporters=[arm],
    )
    return await build_system("gateway-waypoint-build", topology, stores)


async def test_a_transit_waypoint_does_not_fail_the_build() -> None:
    """`safe_transit` names no location, and that must not be an error."""
    build = await _build_with_waypoint()

    assert build.system is not None


async def test_a_transit_waypoint_is_not_a_routing_node() -> None:
    """The arm passes through it; nothing is ever routed TO it."""
    build = await _build_with_waypoint()

    node_ids = {loc.position_id for loc in build.system.system_map.locations}

    assert "safe_transit" not in node_ids
    assert {"pad_1", "pad_2"} <= node_ids


async def test_the_real_destinations_still_route_to_each_other() -> None:
    """Skipping the waypoint must not cost the edge it sits between."""
    build = await _build_with_waypoint()
    system_map = build.system.system_map

    assert system_map.has_any_route("pad_1", "pad_2")
    assert system_map.has_any_route("pad_2", "pad_1")
    # The waypoint is walked by the driver, not routed through the graph.
    assert all(
        "safe_transit" not in path
        for path in system_map.get_all_shortest_any_paths("pad_1", "pad_2")
    )


async def test_a_name_that_is_neither_a_location_nor_a_waypoint_still_fails_loud() -> None:
    """The loud-teachpoint guard stays: only transit poses are exempt."""
    stores = InMemoryRuntimeStoreFactory()
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint("pad_1", _c(100, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("typo_nest", _c(500, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )
    topology = Topology(locations={"pad_1": PlatePad("pad_1")}, transporters=[arm])

    with pytest.raises(ValueError, match="neither a registered location"):
        await build_system("loud-teachpoint-build", topology, stores)
