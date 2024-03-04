
from typing import Dict, List
import conftest
from resource_models.labware import Labware
from routing.router import RouteBuilder
from routing.system_graph import SystemGraph
from tests.mock import MockEquipmentResource, MockRoboticArm
from workflow_models.workflow import LabwareThread, Method, MethodAction

import networkx as nx

class TestSystemGraph:
    def test_get_shortest_path(self, system_graph: SystemGraph):
        expected_path = ["loc_stacker1", "loc_robot1", "loc3", "loc_robot2", "loc_ham1"]
        path = system_graph.get_shortest_any_path("loc_stacker1", "loc_ham1")
        assert path == expected_path

    def test_no_path_through_in_use_plate(self, system_graph: SystemGraph):
        loc3 = system_graph.locations["loc3"]
        loc3.load_labware(Labware("plate", labware_type="mock_labware"))
        try:
            path = system_graph.get_shortest_available_path("loc_stacker1", "loc_ham1")
            assert False
        except nx.NetworkXNoPath:
            assert True
    

class TestRouteBuilder:

    def test_route_builds_correct_path(self, 
                                       system_graph: SystemGraph, 
                                       shaker1: MockEquipmentResource, 
                                       ham1: MockEquipmentResource):
        plate = Labware("plate", "mock_labware")
        ham_method = Method("venus_method", [MethodAction(ham1, "run", [plate], [plate])])
        shaker_method = Method("shaker_method", [MethodAction(shaker1, "shake", [plate], [plate])])
        thread = LabwareThread(name=plate.name,
                               labware=plate,
                               method_sequence=[ham_method, 
                                                shaker_method],
                                start_location=system_graph.locations["stacker1"],
                                end_location=system_graph.locations["loc5"]
        )

        builder = RouteBuilder(thread, system_graph)
        route = builder.get_route()
        
        expected_stops = ["stacker1", "robot1", "loc3", "robot2", "ham1", "loc5"]
        stops = [stop.name for stop in route.path]
        assert stops == expected_stops

    def test_route_assigns_actions_correctly(self, 
                                             system_graph: SystemGraph,
                                             robot1: MockRoboticArm,
                                             robot2: MockRoboticArm, 
                                             stacker1: MockEquipmentResource, 
                                             shaker1: MockEquipmentResource, 
                                             ham1: MockEquipmentResource):
        completed_actions: List[Dict[str, str]] = []
        plate = Labware("plate", "mock_labware")
        ham_method = Method("venus_method", [MethodAction(ham1, "run", [plate], [plate])])
        shaker_method = Method("shaker_method", [MethodAction(shaker1, "shake", [plate], [plate])])
        thread = LabwareThread(name=plate.name,
                               labware=plate,
                               method_sequence=[ham_method, 
                                                shaker_method],
                                start_location=system_graph.locations["stacker1"],
                                end_location=system_graph.locations["loc5"]
        )

        stacker1.set_on_load_labware(lambda x: completed_actions.append({"resource": stacker1.name, "action": "load_labware", "labware": x.name}))
        stacker1.set_on_unload_labware(lambda x: completed_actions.append({"resource": stacker1.name, "action": "unload_labware", "labware": x.name}))
        ham1.set_on_load_labware(lambda x: completed_actions.append({"resource": ham1.name, "action": "load_labware", "labware": x.name}))
        ham1.set_on_unload_labware(lambda x: completed_actions.append({"resource": ham1.name, "action": "unload_labware", "labware": x.name}))
        shaker1.set_on_load_labware(lambda x: completed_actions.append({"resource": shaker1.name, "action": "load_labware", "labware": x.name}))
        shaker1.set_on_unload_labware(lambda x: completed_actions.append({"resource": shaker1.name, "action": "unload_labware", "labware": x.name}))
        robot1.set_on_pick(lambda x, y: completed_actions.append({"resource": robot1.name, "action": "pick", "location": y, "labware": x}))
        robot1.set_on_place(lambda x, y: completed_actions.append({"resource": robot1.name, "action": "place", "location": y, "labware": x}))
        robot2.set_on_pick(lambda x, y: completed_actions.append({"resource": robot2.name, "action": "pick", "location": y, "labware": x}))
        robot2.set_on_place(lambda x, y: completed_actions.append({"resource": robot2.name, "action": "place", "location": y, "labware": x}))

        builder = RouteBuilder(thread, system_graph)
        route = builder.get_route()


        expected_actions = [
            # stacker1 downstack
            {
                "res":"stacker1",
                "action":"unload_labware",
                "labware":"plate"
            },         
            # robot1 pick stacker1   
            {
                "res": "robot1",
                "action": "pick",
                "location": "stacker1",
                "labware": "plate"
            },
            # robot1 place loc3
            {
                "res": "robot1",
                "action": "place",
                "location": "loc3",
                "labware": "plate"
            },
            # loc3 load
            {
                "res": "lo3",
                "action": "load_labware",
                "labware": "plate"
            },
            # loc3 execute
            {
                "res": "loc3", 
                "action":"execute"
            },
             # loc3 unload
            {
                "res":"loc3",
                "action":"unload_labware",
                "labware":"plate"
            },
            
            # robot2 pick loc3
            {
                "res": "robot2",
                "action": "pick",
                "location": "loc3",
                "labware": "plate"
            },
            # robot2 place ham1
            {
                "res":"robot2",
                "action":"place",
                "location":"ham1",
                "labware":"plate"
            },
            # ham1 load
            {
                "res":"ham1",
                "action":"load_labware",
                "labware":"plate"
            },
            # ham1 execute
            {
                "res":"ham1",
                "action":"execute"
            },
            # ham1 unload
            {
                "res":"ham1",
                "action":"unload_labware",
                "labware":"plate"
            },
            # robot2 pick ham1
            {
                "res":"robot2",
                "action":"pick",
                "location":"ham1",
                "labware":"plate"
            },
            # robot2 place loc5
            {
                "res":"robot2",
                "action":"place",
                "location":"loc5",
                "labware":"plate"
            },
            # loc5 load
            {
                "res":"loc5",
                "action":"load_labware",
                "labware":"plate"
            },
            # loc5 execute
            {
                "res":"loc5",
                "action":"execute"
            }
        ]

        