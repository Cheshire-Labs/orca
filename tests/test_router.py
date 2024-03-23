import unittest
from routing.router import Route, Location, EquipmentAction, SystemGraph
from tests.mock import MockRoboticArm
from workflow_models.workflow import LabwareThread




class TestRoute(unittest.TestCase):
    def setUp(self):
        self.robot1 = MockRoboticArm("robot1")
        self.robot2 = MockRoboticArm("robot2")
        # Create a sample SystemGraph for testing
        self.system = SystemGraph()
        self.system.add_location(Location("Location1"))
        self.system.add_location(Location("Location2"))
        self.system.add_location(Location("Location3"))
        self.system.add_edge("Location1", "Location2", self.robot1)
        self.system.add_edge("Location2", "Location3", self.robot2)

    def test_add_stop(self):
        route = Route(Location("Location1"), Location("Location3"))
        action1 = EquipmentAction()
        action2 = EquipmentAction()
        route.add_stop(Location("Location2"), action1)
        route.add_stop(Location("Location3"), action2)
        route.extend_to_location(self.system)
        self.assertEqual(len(route.actions), 2)
        self.assertEqual(route.actions[0].source.teachpoint_name, "Location1")
        self.assertEqual(route.actions[0].target.teachpoint_name, "Location2")
        self.assertEqual(route.actions[1].source.teachpoint_name, "Location2")
        self.assertEqual(route.actions[1].target.teachpoint_name, "Location3")

    def test_build_route(self):
        route = Route(Location("Location1"), Location("Location3"))
        action1 = EquipmentAction()
        action2 = EquipmentAction()
        route.add_stop(Location("Location2"), action1)
        route.add_stop(Location("Location3"), action2)
        route.extend_to_location(self.system)

        self.assertEqual(len(route.actions), 2)

        self.assertEqual(route.actions[0].source.teachpoint_name, "Location1")
        self.assertEqual(route.actions[0].target.teachpoint_name, "Location2")
        self.assertEqual(route.actions[0]._transporter, self.robot1)

        self.assertEqual(route.actions[1].source.teachpoint_name, "Location2")
        self.assertEqual(route.actions[1].target.teachpoint_name, "Location3")
        self.assertEqual(route.actions[1]._transporter, self.robot2)

if __name__ == '__main__':
    unittest.main()