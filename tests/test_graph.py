import asyncio

from orca.resource_models.labware import Labware
from orca.system.system_map import SystemMap


class TestSystemGraph:
    
    def test_get_shortest_path(self, system_map: SystemMap):
        expected_path = ["stacker1", "loc3", "ham1"]
        path = system_map.get_all_shortest_available_paths("stacker1", "ham1")

        assert path[0] == expected_path

    def test_no_path_through_in_use_plate(self, system_map: SystemMap):
        loc3 = system_map.get_location("loc3")
        blocking_labware = Labware("plate", labware_type="mock_labware")
        asyncio.run(loc3.prepare_for_place(blocking_labware))
        asyncio.run(loc3.notify_placed(blocking_labware))
        has_path = system_map.has_available_route("stacker1", "ham1")
        assert not has_path
    
