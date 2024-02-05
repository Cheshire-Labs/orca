from typing import Any, Dict, List, Optional
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


class SystemGraph:
    @property
    def nodes(self) -> Dict[str, Location]:
        nodes = {}
        for name, node in self._graph.nodes.items(): # type: ignore
            nodes[name] = node["location"]
        return nodes

    def __init__(self) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()

    def add_location(self, location: Location) -> None:
        self._graph.add_node(location.name, location=location) # type: ignore

    def add_edge(self, start: str, end: str, action: Action, weight: float = 5.0) -> None:
        if start not in self._graph.nodes:
            raise ValueError(f"Node {start} does not exist")
        if end not in self._graph.nodes:
            raise ValueError(f"Node {end} does not exist")
        self._graph.add_edge(start, end, action=action, weight=weight) # type: ignore

    def set_edge_weight(self, start: str, end: str, weight: float) -> None:
        self._graph[start][end]['weight'] = weight

    def has_available_path(self, source: str, target: str) -> bool:
        available_graph = nx.subgraph(self._graph, self._get_available_locations()) # type: ignore
        return nx.has_path(available_graph, source, target)
    
    def has_any_path(self, source: str, target: str) -> bool:
        return nx.has_path(self._graph, source, target)
    
    def get_shortest_available_path(self, source: str, target: str) -> List[Location]:
        available_graph = nx.subgraph(self._graph, self._get_available_locations())# type: ignore
        path: List[str] = nx.shortest_path(available_graph, source, target) # type: ignore
        return [self.nodes[name] for name in path]
    
    def get_shortest_any_path(self, source: str, target: str) -> List[Location]:
        path: List[str] = nx.shortest_path(self._graph, source, target) # type: ignore
        return [self.nodes[name] for name in path]
    
    def get_blocking_locations(self, source: str, target: str) -> List[Location]:
        # TODO: add input validations of source and target entered
        locaitons: List[Location] = []
        for location in self.get_shortest_any_path(source, target):
            if location.in_use or location.reserved:
                locaitons.append(location)
        return locaitons

    def _get_available_locations(self) -> Dict[str, Dict[str, Any]]:
        nodes = {}
        for node, nodedata in self._graph.nodes.items():
            location: Location = nodedata["location"]
            if not location.in_use and not location.reserved:
                nodes[node] = nodedata
        return nodes
