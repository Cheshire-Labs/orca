from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from drivers.ilabware_transporter import ILabwareTransporter
from location import Location
from method import Action, Method
from system import System
from workflow import Workflow
import networkx as nx

class CandidateActionCurator:
    """Replaces actions and sorts them according to determine what should be completede first"""
    def __init__(self, system: System) -> None:
        self._system = system

    def _sort(self, actions: List[Action]) -> List[Action]:
        # TODO: implement sorting algorithms
        return actions
    
    def _replace(self, actions: List[Action]) -> List[Action]:
        # TODO: set algorithms to replace actions with move actions when the plate isn't at the location yet
        return actions

    def get_curated_actions(self, actions: List[Action]) -> List[Action]:
        actions = self._replace(actions)
        actions = self._sort(actions)
        return actions
    
class Router:
    def __init__(self, system: System):
        self._system: System = system
        self._workflow: Optional[Workflow] = None
        self._curator = CandidateActionCurator(self._system)

    def set_method(self, method: Method) -> None:
        self._method = method
        candidate_next_action = method.get_next_action()
        self._candidate_actions = [candidate_next_action] if candidate_next_action is not None else []

    def set_workflow(self, workflow: Workflow) -> None:
        self._workflow = workflow

    def _get_candidate_actions(self) -> List[Action]:
        if self._workflow is None:
            raise ValueError("A workflow has not been set.  A workflow must be set before Router can determine the next action")
        candidate_actions: List[Action] = []
        for _, labware_thread in self._workflow.labware_threads.items():
            candidate_next_method = labware_thread.get_next_method()
            if candidate_next_method is not None:
                method_action = candidate_next_method.get_next_action()
                if method_action is not None:
                    candidate_actions.append(method_action)
        return candidate_actions

    def get_next_action(self) -> Action | None:
        curated_actions = self._curator.get_curated_actions(self._get_candidate_actions())
        if len(curated_actions) == 0:
            return None
        return curated_actions[0]


class RouteSingleStep:
    def __init__(self, source: Location, target: Location, weight: float, action: Action) -> None:
        self._source = source
        self._target = target
        self._weight = weight
        self._action = action

    @property
    def source(self) -> Location:
        return self._source

    @property
    def target(self) -> Location:
        return self._target

    @property
    def action(self) -> Action:
        return self._action
    
class Route:
    def __init__(self, steps: List[RouteSingleStep]) -> None:
        self._steps = steps
        self._source = steps[0].source
        self._target = steps[-1].target

    @property
    def steps(self) -> List[RouteSingleStep]:
        return self._steps
    
    @property
    def source(self) -> Location:
        return self._source
    
    @property
    def target(self) -> Location:
        return self._target
    

class _NetworkXHandler:
    
    def __init__(self, graph: Optional[nx.DiGraph] = None) -> None:
        if graph is None:
            graph = nx.DiGraph()
        self._graph: nx.DiGraph = graph
    
    def add_node(self, name: str, location: Location) -> None:
        self._graph.add_node(name, location=location) # type: ignore
    
    def add_edge(self, start: str, end: str, action: Action, weight: float = 5.0) -> None:
        self._graph.add_edge(start, end, action=action, weight=weight) # type: ignore

    def has_path(self, source: str, target: str) -> bool:
        return nx.has_path(self._graph, source, target) # type: ignore

    def get_nodes(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._graph.nodes.items())
    
    def get_node_data(self, name: str) -> Dict[str, Any]:
        return self._graph.nodes[name]
    
    def get_shortest_path(self, source: str, target: str) -> List[str]:
        path: List[str] = nx.shortest_path(self._graph, source, target) # type: ignore
        return path

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

class SystemGraph:
    @property
    def nodes(self) -> Dict[str, Location]:
        nodes = {}
        for name, node in self._graph.get_nodes().items(): 
            nodes[name] = node["location"]
        return nodes

    def __init__(self) -> None:
        self._graph: _NetworkXHandler = _NetworkXHandler()

    def add_location(self, location: Location) -> None:
        self._graph.add_node(location.name, location)

    def add_edge(self, start: str, end: str, action: Action, weight: float = 5.0) -> None:
        if start not in self._graph.get_nodes():
            raise ValueError(f"Node {start} does not exist")
        if end not in self._graph.get_nodes():
            raise ValueError(f"Node {end} does not exist")
        self._graph.add_edge(start, end, action=action, weight=weight) 

    def set_edge_weight(self, start: str, end: str, weight: float) -> None:
        self._graph[start][end]['weight'] = weight

    def has_available_path(self, source: str, target: str) -> bool:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return self._graph.has_path(source, target)
    
    def has_any_path(self, source: str, target: str) -> bool:
        return self._graph.has_path(source, target)
    
    def get_shortest_any_route(self, source: str, target: str) -> Route:
        path: List[str] = self._get_shortest_any_path(source, target)
        return self._get_route_from_path(path)
    
    def get_shortest_available_route(self, source: str, target: str) -> Route:
        path: List[str] = self._get_shortest_available_path(source, target)
        return self._get_route_from_path(path)
    
    def _get_shortest_available_path(self, source: str, target: str) -> List[str]:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return available_graph.get_shortest_path(source, target)
    
    def _get_shortest_any_path(self, source: str, target: str) -> List[str]:
        return self._graph.get_shortest_path(source, target)

    def _get_route_from_path(self, path: List[str]) -> Route:
        path_graph = self._graph.get_path_graph(path)
        steps: List[RouteSingleStep] = []
        for start_name, end_name, data in path_graph.get_all_edges():
            start = self._graph.get_node_data(start_name)["location"]
            end = self._graph.get_node_data(end_name)["location"]
            steps.append(RouteSingleStep(start, end, data["weight"], data["action"]))
        return Route(steps)

    def get_blocking_locations(self, source: str, target: str) -> List[Location]:
        # TODO: add input validations of source and target entered
        locaitons: List[Location] = []
        for location_name in self._get_shortest_any_path(source, target):
            location: Location = self._graph.get_node_data(location_name)["location"]
            if location.in_use or location.reserved:
                locaitons.append(location)
        return locaitons

    def _get_available_locations(self) -> Dict[str, Dict[str, Any]]:
        nodes = {}
        for node, nodedata in self._graph.get_nodes().items():
            location: Location = nodedata["location"]
            if not location.in_use and not location.reserved:
                nodes[node] = nodedata
        return nodes
