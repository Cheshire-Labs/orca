
from resource_models.location import Location

import pytest

from routing.system_graph import SystemGraph
from tests.mock import MockEquipmentResource, MockRoboticArm



@pytest.fixture(scope='session', autouse=True)
def robot1(request) -> MockRoboticArm:
    return MockRoboticArm("robot1", mocking_type="robot") # loc1, loc2, loc3, stacker1, shaker1

@pytest.fixture(scope='session', autouse=True)
def robot2(request) -> MockRoboticArm:
    return MockRoboticArm("robot2", mocking_type="robot") # loc3, loc4, loc5, ham1

@pytest.fixture(scope='session', autouse=True)
def stacker1(request) -> MockEquipmentResource:
    return MockEquipmentResource("stacker1", mocking_type="stacker")

@pytest.fixture(scope='session', autouse=True)
def shaker1(request) -> MockEquipmentResource:
    return MockEquipmentResource("shaker1", mocking_type="shaker")

@pytest.fixture(scope='session', autouse=True)
def ham1(request) -> MockEquipmentResource:
    return MockEquipmentResource("ham1", mocking_type="hamilton")

@pytest.fixture(scope='session', autouse=True)
def system_graph(robot1, robot2, stacker1, shaker1, ham1) -> SystemGraph:
    graph = SystemGraph()

    # create Locations
    loc1 = Location("loc1")
    loc2 = Location("loc2")
    loc3 = Location("loc3")
    loc4 = Location("loc4")
    loc5 = Location("loc5")
    loc6 = Location("loc6")
    loc_robot1 = Location("robot1")
    loc_robot1.set_resource(robot1)
    loc_robot2 = Location("robot2")
    loc_robot2.set_resource(robot2)
    loc_stacker1 = Location("stacker1")
    loc_stacker1.set_resource(stacker1)
    loc_shaker1 = Location("shaker1")
    loc_shaker1.set_resource(shaker1)
    loc_ham1 = Location("ham1")
    loc_ham1.set_resource(ham1)
    
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
    graph.add_location(loc_shaker1)
    graph.add_location(loc_ham1)

    # Add edges with actions
    # loc3 is the connecting location
    # robot1 -> [loc1, loc2, loc3, stacker1, shaker1]
    graph.add_edge("robot1", "loc1")
    graph.add_edge("robot1", "loc2")
    graph.add_edge("robot1", "loc3")
    graph.add_edge("robot1", "stacker1")
    graph.add_edge("robot1", "shaker1")
    graph.add_edge("loc1", "robot1")
    graph.add_edge("loc2", "robot1")
    graph.add_edge("loc3", "robot1")
    graph.add_edge("stacker1", "robot1")
    graph.add_edge("shaker1", "robot1")

    # robot2 -> [loc3, loc4, loc5, ham1]
    graph.add_edge("robot2", "loc3")
    graph.add_edge("robot2", "loc4")
    graph.add_edge("robot2", "loc5")
    graph.add_edge("robot2", "ham1")
    graph.add_edge("loc3", "robot2")
    graph.add_edge("loc4", "robot2")
    graph.add_edge("loc5", "robot2")
    graph.add_edge("ham1", "robot2")

    return graph