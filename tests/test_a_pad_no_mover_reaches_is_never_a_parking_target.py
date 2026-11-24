"""A pad no mover reaches is left out of the parking pads; it does not stop the search for the pads that are reached."""

from orca.sdk.system import ResourceRegistry, SystemMap
from tests.test_helpers import create_test_transporter, wire_system_map


async def test_an_unreached_pad_is_skipped_and_the_reached_pads_are_returned() -> None:
    transporter = create_test_transporter("robot1", ["pad1", "pad2"])
    registry = ResourceRegistry()
    registry.add_resource(transporter)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, pads=["pad1", "pad2", "island"])

    paths = system_map.get_shortest_paths_to_deadlock_resolution("pad1")

    assert {path[-1] for path in paths} == {"pad2"}
