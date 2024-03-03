from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from resource_models.base_resource import TransporterResource
from resource_models.labware import Labware
from resource_models.location import Location
from resource_models.location import Location
import networkx as nx

from system import System
from workflow_models.action import BaseAction, IAction, LoadLabwareAction, NullAction, PickAction, PlaceAction, UnloadLabwareAction

class _NetworkXHandler:
    
    def __init__(self, graph: Optional[nx.DiGraph] = None) -> None: # type: ignore
        if graph is None:
            graph = nx.DiGraph()
        self._graph: nx.DiGraph = graph
    
    def add_node(self, name: str, location: Location) -> None:
        self._graph.add_node(name, location=location) # type: ignore
    
    def add_edge(self, start: str, end: str, weight: float = 5.0) -> None:
        self._graph.add_edge(start, end, weight=weight) # type: ignore

    def has_path(self, source: str, target: str) -> bool:
        return nx.has_path(self._graph, source, target) # type: ignore

    def get_nodes(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._graph.nodes.items())
    
    def get_node_data(self, name: str) -> Dict[str, Any]:
        return self._graph.nodes[name]
    
    def get_shortest_path(self, source: str, target: str) -> List[str]:
        path: List[str] = nx.shortest_path(self._graph, source, target) # type: ignore
        return path

    def get_all_shortest_paths(self, source: str, target: str) -> List[List[str]]:
        return list(nx.all_shortest_paths(self._graph, source, target)) # type: ignore

    def get_subgraph(self, nodes: List[str]) -> _NetworkXHandler:
        return _NetworkXHandler(nx.subgraph(self._graph, nodes)) # type: ignore
    
    def get_path_graph(self, path: List[str]) -> _NetworkXHandler:
        return _NetworkXHandler(nx.path_graph(self._graph, path)) # type: ignore
    
    def get_all_edges(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Returns all edges with data as a list of tuples (source, target, data)"""
        return list(self._graph.edges(data=True))

    def get_edge_data(self, source: str, target: str) -> Dict[str, Any]:
        return self._graph.edges[source, target]

    def __getitem__(self, key: str) -> Dict[str, Any]:
        return self._graph.nodes[key]

# class RouteSingleStep:
#     def __init__(self, source: Location, target: Location, weight: float, action: IAction) -> None:
#         self._source = source
#         self._target = target
#         self._weight = weight
#         self._action = action

#     @property
#     def source(self) -> Location:
#         return self._source

#     @property
#     def target(self) -> Location:
#         return self._target

#     @property
#     def action(self) -> IAction:
#         return self._action
    
class RouteAction(BaseAction):
    def __init__(self, source: Location, target: Location) -> None:
        super().__init__()
        self._source = source
        self._target = target
        self._labware: Optional[Labware] = None

    def set_labware(self, labware: Labware) -> None:
        self._labware = labware

    def get_actions(self) -> List[IAction]:
        if self._labware is None:
                raise ValueError("Labware must be set before getting actions")

        actions: List[IAction] = []
        self._append_source_actions(actions)
        self._append_target_actions(actions)
       
            


        return actions
    
    def _append_source_actions(self, actions: List[IAction]) -> None:
        if self._source is None:
            # assume action is to create labware
            actions.append(CreateLabwareAction(self._labware, self._target.name))
        elif self._source.resource is None:
            actions.append(NullAction())
        elif self._source.resource is TransporterResource:
            # coming from robot
            actions.append(PlaceAction(self._source.resource, self._labware, self._target.name))
        else:
            actions.append(UnloadLabwareAction(self._source.resource, self._labware))

    def _create_target_actions(self, actions: List[IAction]) -> None:
        if self._target is None:
            #assume action is end of labware route
            actions.append(FinalLabwareAction(self._labware, self._source.name))
        elif self._target.resource is None:
            actions.append(NullAction())
        elif self._target.resource is TransporterResource:
            # going to robot
            actions.append(PickAction(self._target.resource, self._labware, self._source.name))
        else:
            actions.append(LoadLabwareAction(self._target.resource, self._labware))

    
    def _perform_action(self) -> None:
        [action.execute() for action in self.get_actions()]
        

class Route:
    def __init__(self) -> None:
        self._edges: List[RouteAction] = []

    def add_stop(self, location: str, action: BaseAction, system: SystemGraph) -> None:
        # TODO:  handle in route builder
        if len(self._edges) == 0:
            self._edges.append(CreateLabwareAct)
        else:
            if self._edges[-1].location != location:
                self._bridge_stops(location, action, system)
            self._edges.append(RouteStop(location, action))

    def _bridge_stops(self, location: str, action: BaseAction, system: SystemGraph) -> None:
        # TODO:  handle in route builder
        last_location = self._edges[-1].location
        if not system.has_available_route(last_location, location):
            blocking_locations = system.get_blocking_locations(last_location, location)
            if len(blocking_locations) == 0:
                raise ValueError(f"No available route from {last_location} to {location}")
            else:
                raise ValueError(f"Location {blocking_locations[0].name} is blocking the route from {last_location} to {location}")
        
        path = system.get_shortest_available_path(last_location, location)
        # then add a stop for each with the action to move the labware
        for location in path:
            self.add_stop(location, NullAction(), system)
        self._stops.append(RouteStop(location, action))


class SystemGraph:

    def __init__(self) -> None:
        self._graph: _NetworkXHandler = _NetworkXHandler()

    @property
    def locations(self) -> Dict[str, Location]:
        nodes = {}
        for name, node in self._graph.get_nodes().items(): 
            nodes[name] = node["location"]
        return nodes
    
    def add_location(self, location: Location) -> None:
        self._graph.add_node(location.name, location)

    def add_edge(self, start: str, end: str, weight: float = 5.0) -> None:
        if start not in self._graph.get_nodes():
            raise ValueError(f"Node {start} does not exist")
        if end not in self._graph.get_nodes():
            raise ValueError(f"Node {end} does not exist")
        self._graph.add_edge(start, end, weight=weight) 

    def set_edge_weight(self, start: str, end: str, weight: float) -> None:
        self._graph[start][end]['weight'] = weight

    def has_available_route(self, source: str, target: str) -> bool:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return available_graph.has_path(source, target)
    
    def has_any_route(self, source: str, target: str) -> bool:
        return self._graph.has_path(source, target)
    
    def get_shortest_any_route(self, source: str, target: str) -> Route:
        path: List[str] = self._get_shortest_any_path(source, target)
        return self._get_route_from_path(path)
    
    def get_all_shortest_any_routes(self, source: str, target: str) -> List[Route]:
        paths: List[List[str]] = self._get_all_shortest_any_paths(source, target)
        return [self._get_route_from_path(path) for path in paths]
    
    def get_shortest_available_route(self, source: str, target: str) -> Route:
        path: List[str] = self._get_shortest_available_path(source, target)
        return self._get_route_from_path(path)
    
    # def get_shortest_available_route(self, locations: List[str]) -> Route:
    #     path: List[str] = []
    #     start_location = locations.pop(0)
    #     for location in locations:
    #         if not self._graph.has_path(start_location, location):
    #             raise ValueError(f"No available route from {start_location} to {location}")
    #         path_segment = self._get_shortest_available_path(start_location, location)
    #         path.extend(path_segment)
    #         start_location = location
    #     return self._get_route_from_path(path)
    
    def get_all_shortest_available_routes(self, source: str, target: str) -> List[Route]:
        paths: List[List[str]] = self._get_all_shortest_available_paths(source, target)
        return [self._get_route_from_path(path) for path in paths]
    
    
    
    def get_blocking_locations(self, source: str, target: str) -> List[Location]:
        # TODO: add input validations of source and target entered
        locations: List[Location] = []
        for location_name in self._get_shortest_any_path(source, target):
            location: Location = self._graph.get_node_data(location_name)["location"]
            if location.in_use:
                locations.append(location)
        return locations
    
    def get_all_blocking_locations(self, source: str, target: str) -> List[Location]:
        locations: List[Location] = []
        for path in self._get_all_shortest_any_paths(source, target):
            for location_name in path:
                location: Location = self._graph.get_node_data(location_name)["location"]
                if location.in_use:
                    locations.append(location)
        return locations
    
    def _get_shortest_available_path(self, source: str, target: str) -> List[str]:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return available_graph.get_shortest_path(source, target)
    
    def _get_all_shortest_available_paths(self, source: str, target: str) -> List[List[str]]:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return available_graph.get_all_shortest_paths(source, target)
    
    def _get_shortest_any_path(self, source: str, target: str) -> List[str]:
        return self._graph.get_shortest_path(source, target)
    
    def _get_all_shortest_any_paths(self, source: str, target: str) -> List[List[str]]:
        return self._graph.get_all_shortest_paths(source, target)

    def _get_route_from_path(self, path: List[str]) -> Route:
        path_graph = self._graph.get_path_graph(path)
        steps: List[RouteSingleStep] = []
        for start_name, end_name, data in path_graph.get_all_edges():
            start = self._graph.get_node_data(start_name)["location"]
            end = self._graph.get_node_data(end_name)["location"]
            steps.append(RouteSingleStep(start, end, data["weight"], data["action"]))
        return Route(steps)


    def _get_available_locations(self) -> Dict[str, Dict[str, Any]]:
        nodes = {}
        for node, nodedata in self._graph.get_nodes().items():
            location: Location = nodedata["location"]
            if not location.in_use:
                nodes[node] = nodedata
        return nodes
