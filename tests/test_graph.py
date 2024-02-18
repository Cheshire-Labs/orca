
from drivers.drivers import MockRoboticArm, MockResource
from location import Location
from Action import IAction
from router import RouteSingleStep, SystemGraph
import networkx as nx

import pytest

@pytest.fixture
def system_graph() -> SystemGraph:
    graph = SystemGraph()

    robot1 = MockRoboticArm("robot1") # loc1, loc2, loc3
    robot2 = MockRoboticArm("robot2") # loc3, loc4, loc5
    stacker1 = MockResource("stacker1", mocking_type="stacker")
    ham1 = MockResource("ham1", mocking_type="hamilton")

    # create Locations
    loc1 = Location("loc1")
    loc2 = Location("loc2")
    loc3 = Location("loc3")
    loc4 = Location("loc4")
    loc5 = Location("loc5")
    loc6 = Location("loc6")
    loc_robot1 = Location("loc_robot1")
    loc_robot1.set_resource(robot1)
    loc_robot2 = Location("loc_robot2")
    loc_robot2.set_resource(robot2)
    loc_stacker1 = Location("loc_stacker1")
    loc_stacker1.set_resource(stacker1)
    loc_ham1 = Location("loc_ham1")
    loc_ham1.set_resource(ham1)

    # create Actions
    robot1_pick_action = IAction(robot1, "pick")
    robot1_place_action = IAction(robot1, "place")
    robot2_pick_action = IAction(robot2, "pick")
    robot2_place_action = IAction(robot2, "place")

    stacker_upstack_action = IAction(stacker1, "upstack")
    stacker_load_action = IAction(stacker1, "load")
    # stacker_load_action.set_next_action(stacker_upstack_action)
    stacker_downstack_action = IAction(stacker1, "downstack")
    # stacker_unload_action.set_previous_action(stacker_downstack_action)
    
    # add locations to graph
    graph.add_location(loc1)
    graph.add_location(loc2)
    graph.add_location(loc3)
    graph.add_location(loc4)
    graph.add_location(loc5)
    graph.add_location(loc6)
    graph.add_location(loc_robot1)
    graph.add_location(loc_robot2)
    graph.add_location(loc_stacker1)
    graph.add_location(loc_ham1)

    # Add edges with actions
    # robot1 -> [loc1, loc2, loc3, stacker1]
    graph.add_edge("loc_robot1", "loc1", robot1_place_action)
    graph.add_edge("loc_robot1", "loc2", robot1_place_action)
    graph.add_edge("loc_robot1", "loc3", robot1_place_action)
    graph.add_edge("loc_robot1", "loc_stacker1", robot1_pick_action)
    graph.add_edge("loc1", "loc_robot1", robot1_pick_action)
    graph.add_edge("loc2", "loc_robot1", robot1_pick_action)
    graph.add_edge("loc3", "loc_robot1", robot1_pick_action)
    graph.add_edge("loc_stacker1", "loc_robot1", robot1_pick_action)

    # robot2 -> [loc3, loc4, loc5, ham1]
    graph.add_edge("loc_robot2", "loc3", robot2_place_action)
    graph.add_edge("loc_robot2", "loc4", robot2_place_action)
    graph.add_edge("loc_robot2", "loc5", robot2_place_action)
    graph.add_edge("loc_robot2", "loc_ham1", robot2_place_action)
    graph.add_edge("loc3", "loc_robot2", robot2_pick_action)
    graph.add_edge("loc4", "loc_robot2", robot2_pick_action)
    graph.add_edge("loc5", "loc_robot2", robot2_pick_action)
    graph.add_edge("loc_ham1", "loc_robot2", robot2_pick_action)


    return graph


def test_get_shortest_path(system_graph: SystemGraph):
    expected_path = ["loc_stacker1", "loc_robot1", "loc3", "loc_robot2", "loc_ham1"]
    path = system_graph._get_shortest_any_path("loc_stacker1", "loc_ham1")
    assert path == expected_path

def test_no_path_through_in_use_plate(system_graph: SystemGraph):
    loc3 = system_graph.nodes["loc3"]
    loc3.in_use = True
    try:
        path = system_graph._get_shortest_available_path("loc_stacker1", "loc_ham1")
        assert False
    except nx.NetworkXNoPath:
        assert True
