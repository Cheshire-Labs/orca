
from typing import Dict, List
import conftest
from method_executor import MethodExecutor
from resource_models.labware import Labware
from resource_models.resource_pool import EquipmentResourcePool
from system.system_map import SystemMap
from tests.mock import MockEquipmentResource, MockRoboticArm
from workflow_models.method_action import DynamicResourceAction, LocationAction
from workflow_models.workflow import LabwareThread, Method

import networkx as nx


class TestSystemGraph:
    def test_get_shortest_path(self, system_map: SystemMap):
        expected_path = ["stacker1", "loc3", "ham1"]
        path = system_map.get_shortest_any_path("stacker1", "ham1")
        assert path == expected_path

    def test_no_path_through_in_use_plate(self, system_map: SystemMap):
        loc3 = system_map.get_location("loc3")
        blocking_labware = Labware("plate", labware_type="mock_labware")
        loc3.prepare_for_place(blocking_labware)
        loc3.notify_placed(blocking_labware)
        try:
            path = system_map.get_shortest_available_path("stacker1", "ham1")
            assert False
        except nx.NetworkXNoPath:
            assert True
    

class TestRouteBuilder:

    def test_route_builds_correct_path(self, 
                                       system_map: SystemMap, 
                                       shaker1: MockEquipmentResource, 
                                       ham1: MockEquipmentResource):
        plate = Labware("plate", "mock_labware")
        ham_method = Method("venus_method")
        ham_method.append_step(DynamicResourceAction(EquipmentResourcePool("ham1", [ham1]), 
                                                     "run", 
                                                     [plate], 
                                                     []))
        shaker_method = Method("shaker_method")
        shaker_method.append_step(DynamicResourceAction(EquipmentResourcePool("shaker1", [shaker1]), 
                                                        "shake", 
                                                        [plate], 
                                                        [])
                                   )
        thread = LabwareThread(plate.name,
                               plate,
                                system_map.get_location("stacker1"),
                                system_map.get_location("loc5"),
                                system_map
        )


        executer = MethodExecutor(ham_method, system_map, system_map.get_locations("loc3"), system_map.get_locations("loc3"))
        method_sequence=[ham_method, 
                                                shaker_method],

        builder = RouteBuilder(thread, system_map)
        route = builder.get_route()
        
        expected_stops = [
            "stacker1", 
            "loc3", 
            "ham1",  
            "loc3",  
            "shaker1", 
            "loc3", 
            "loc5"]
        stops = [stop.teachpoint_name for stop in route.path]
        assert stops == expected_stops

    def test_route_assigns_actions_correctly(self, 
                                             system_map: SystemMap,
                                             robot1: MockRoboticArm,
                                             robot2: MockRoboticArm, 
                                             stacker1: MockEquipmentResource, 
                                             shaker1: MockEquipmentResource, 
                                             ham1: MockEquipmentResource):
        completed_actions: List[Dict[str, str]] = []
        plate = Labware("plate", "mock_labware")
        labware_instance_matcher = LabwareInstanceMatcher([plate])
        ham_method = Method("venus_method")
        ham_method.append_step(LocationAction(EquipmentResourcePool("ham1", [ham1]), 
                                                                  "run", 
                                                                  LabwareInputManager([labware_instance_matcher]), 
                                                        []))
        thread = LabwareThread(name=plate.name,
                                labware=plate,
                                method_sequence=[ham_method],
                                start_location=system_map.locations["stacker1"],
                                end_location=system_map.locations["loc5"]
        )

        stacker1._on_load_labware = lambda x: completed_actions.append({"resource": stacker1.name, "action": "load_labware", "labware": x.name})
        stacker1._on_unload_labware = lambda x: completed_actions.append({"resource": stacker1.name, "action": "unload_labware", "labware": x.name})
        ham1._on_load_labware = lambda x: completed_actions.append({"resource": ham1.name, "action": "load_labware", "labware": x.name})
        ham1._on_unload_labware = lambda x: completed_actions.append({"resource": ham1.name, "action": "unload_labware", "labware": x.name})
        ham1._on_execute = lambda x: completed_actions.append({"resource": ham1.name, "action": x})
        shaker1._on_load_labware = lambda x: completed_actions.append({"resource": shaker1.name, "action": "load_labware", "labware": x.name})
        shaker1._on_unload_labware = lambda x: completed_actions.append({"resource": shaker1.name, "action": "unload_labware", "labware": x.name})
        robot1._on_pick = lambda x, y: completed_actions.append({"resource": robot1.name, "action": "pick", "location": y.teachpoint_name, "labware": x.name})
        robot1._on_place = lambda x, y: completed_actions.append({"resource": robot1.name, "action": "place", "location": y.teachpoint_name, "labware": x.name})
        robot2._on_pick = lambda x, y: completed_actions.append({"resource": robot2.name, "action": "pick", "location": y.teachpoint_name, "labware": x.name})
        robot2._on_place = lambda x, y: completed_actions.append({"resource": robot2.name, "action": "place", "location": y.teachpoint_name, "labware": x.name})


        # system_map.draw()
        
        builder = RouteBuilder(thread, system_map)
        
        stacker1._loaded_labware = [plate]
        route = builder.get_route()
        for action in route.actions:
            action.set_labware(plate)
            action.execute() 


        expected_actions = [
            # stacker1 downstack
            {
                "resource":"stacker1",
                "action":"unload_labware",
                "labware": plate.name
            },         
            # robot1 pick stacker1   
            {
                "resource": "robot1",
                "action": "pick",
                "location": "stacker1",
                "labware": plate.name
            },
            # robot1 place loc3
            {
                "resource": "robot1",
                "action": "place",
                "location": "loc3",
                "labware": plate.name
            },
            # robot2 pick loc3
            {
                "resource": "robot2",
                "action": "pick",
                "location": "loc3",
                "labware": plate.name
            },
            # robot2 place ham1
            {
                "resource":"robot2",
                "action":"place",
                "location":"ham1",
                "labware":plate.name
            },
            # ham1 load
            {
                "resource":"ham1",
                "action":"load_labware",
                "labware":plate.name
            },
            # ham1 execute
            {
                "resource":"ham1",
                "action":"run"
            },
            # ham1 unload
            {
                "resource":"ham1",
                "action":"unload_labware",
                "labware":plate.name
            },
            # robot2 pick ham1
            {
                "resource":"robot2",
                "action":"pick",
                "location":"ham1",
                "labware":plate.name
            },
            # robot2 place loc5
            {
                "resource":"robot2",
                "action":"place",
                "location":"loc5",
                "labware":plate.name
            }
        ]

        assert completed_actions == expected_actions

        