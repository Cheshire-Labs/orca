import pytest

from orca.resource_models.transporter import Transporter
from orca.system.registries import LabwareRegistry
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from tests.test_helpers import create_test_transporter, create_test_device


@pytest.fixture
def system_map() -> SystemMap:
    """
    Create a system map for test_graph.py tests.
    Recreates the graph structure from the original fixture.
    """
    # Create robots with teachpoints matching original test expectations
    robot1 = create_test_transporter("robot1", ["loc1", "loc2", "loc3", "stacker1", "shaker1"])
    robot2 = create_test_transporter("robot2", ["loc3", "loc4", "loc5", "ham1"])

    # Create devices
    stacker1 = create_test_device("stacker1")
    shaker1 = create_test_device("shaker1")
    ham1 = create_test_device("ham1")

    # Create registry and add resources
    registry = ResourceRegistry()
    registry.add_resource(robot1)
    registry.add_resource(robot2)
    registry.add_resource(stacker1)
    registry.add_resource(shaker1)
    registry.add_resource(ham1)

    # Create system map
    system_map = SystemMap(registry)

    # Assign devices to locations
    system_map.assign_resource_to_location("stacker1", stacker1)
    system_map.assign_resource_to_location("shaker1", shaker1)
    system_map.assign_resource_to_location("ham1", ham1)

    return system_map