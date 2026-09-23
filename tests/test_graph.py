from tests.mock import EXTERNAL_MOVER
import asyncio

from orca.resource_models.labware import LabwareInstance
from orca.system.system_map import SystemMap


class TestSystemGraph:
    
    def test_get_shortest_path(self, system_map: SystemMap):
        expected_path = ["stacker1/slot", "loc3", "ham1/slot"]
        path = system_map.get_all_shortest_available_paths("stacker1/slot", "ham1/slot")

        assert path[0] == expected_path

    def test_no_path_through_in_use_plate(self, system_map: SystemMap):
        loc3 = system_map.get_location("loc3")
        blocking_labware = LabwareInstance("plate", labware_type="mock_labware")
        asyncio.run(loc3.prepare_for_place(blocking_labware, EXTERNAL_MOVER))
        asyncio.run(loc3.notify_placed(blocking_labware, EXTERNAL_MOVER))
        has_path = system_map.has_available_route("stacker1/slot", "ham1/slot")
        assert not has_path
    


class TestAddEdgeShadowing:
    """DiGraph holds one transporter per node
    pair. A second mover on an already-served pair must be kept out of the graph
    (first writer wins) and logged, not silently swapped in and not rejected
    with a raise -- ddr_1 reaching both mlstar_1 carriers is a legitimate
    topology and a raise would break hamilton_smc."""

    def test_second_mover_on_a_pair_is_shadowed_and_warned(
        self, system_map: SystemMap, caplog
    ) -> None:
        from tests.test_helpers import create_test_transporter

        first = system_map.get_transporter_between("loc1", "loc2")
        intruder = create_test_transporter("intruder", ["loc1", "loc2"])

        with caplog.at_level("WARNING", logger="orca"):
            asyncio.run(system_map.add_edge("loc1", "loc2", intruder))

        assert system_map.get_transporter_between("loc1", "loc2") is first, (
            "the first writer must keep the edge; a second mover cannot silently "
            "take over the pair")
        assert any("shadowed" in r.getMessage() for r in caplog.records), (
            "the shadowing must be logged, not silent")

    def test_re_adding_the_same_mover_is_idempotent_and_quiet(
        self, system_map: SystemMap, caplog
    ) -> None:
        first = system_map.get_transporter_between("loc1", "loc2")
        with caplog.at_level("WARNING", logger="orca"):
            asyncio.run(system_map.add_edge("loc1", "loc2", first))
        assert system_map.get_transporter_between("loc1", "loc2") is first
        assert not [r for r in caplog.records if "shadowed" in r.getMessage()], (
            "re-adding the incumbent is not a shadowing and must not warn")
