import pytest

try:
    from orca.resource_models.transporter import Transporter
    from tests.mock import MockEquipmentResource, MockRoboticArm
    TRANSPORTER_AVAILABLE = True
except ImportError as e:
    # pylabrobot dependencies may not be fully available
    TRANSPORTER_AVAILABLE = False
    MockRoboticArm = None
    MockEquipmentResource = None

from orca.system.registries import LabwareRegistry
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap



@pytest.fixture
def robot1(request) -> Transporter:
    robot1 = MockRoboticArm("robot1", "robot", ["loc1", "loc2", "loc3", "stacker1", "shaker1"]) # loc1, loc2, loc3, stacker1, shaker1
    return robot1

@pytest.fixture
def robot2(request) -> MockRoboticArm:
    robot2 = MockRoboticArm("robot2", "robot", ["loc3", "loc4", "loc5", "ham1"]) # loc3, loc4, loc5, ham1
    return robot2

@pytest.fixture
def stacker1(request) -> MockEquipmentResource:
    return MockEquipmentResource("stacker1", mocking_type="stacker")

@pytest.fixture
def shaker1(request) -> MockEquipmentResource:
    return MockEquipmentResource("shaker1", mocking_type="shaker")

@pytest.fixture
def ham1(request) -> MockEquipmentResource:
    return MockEquipmentResource("ham1", mocking_type="hamilton")

@pytest.fixture
def resource_registry(request, robot1, robot2, stacker1, shaker1, ham1) -> ResourceRegistry:
    registry = ResourceRegistry()
    for resource in [robot1, robot2, stacker1, shaker1, ham1]:
        registry.add_resource(resource)
    return registry

@pytest.fixture
def labware_registry(request) -> LabwareRegistry:
    return LabwareRegistry()


@pytest.fixture
def system_map(resource_registry: ResourceRegistry,
               robot1: MockRoboticArm, 
               robot2: MockRoboticArm, 
               stacker1: MockEquipmentResource, 
               shaker1: MockEquipmentResource, 
               ham1: MockEquipmentResource) -> SystemMap:
    graph = SystemMap(resource_registry)
    return graph