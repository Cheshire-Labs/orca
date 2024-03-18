from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from resource_models.base_resource import LabwareLoadable
from resource_models.location import Location
import networkx as nx
import matplotlib.pyplot as plt

from resource_models.transporter_resource import TransporterResource


class _NetworkXHandler:
    
    def __init__(self, graph: Optional[nx.DiGraph] = None) -> None: # type: ignore
        if graph is None:
            graph = nx.DiGraph()
        self._graph: nx.DiGraph = graph
    
    def add_node(self, name: str, location: Location) -> None:
        self._graph.add_node(name, location=location) # type: ignore
    
    def add_edge(self, start: str, end: str, transporter: TransporterResource, weight: float = 1.0) -> None:
        self._graph.add_edge(start, end, weight=weight, transporter=transporter) # type: ignore

    def has_path(self, source: str, target: str) -> bool:
        return nx.has_path(self._graph, source, target) # type: ignore

    def get_nodes(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._graph.nodes.items())
    
    def get_node_data(self, name: str) -> Dict[str, Any]:
        return self._graph.nodes[name]
    
    def get_shortest_path(self, source: str, target: str) -> List[str]:
        path: List[str] = nx.shortest_path(self._graph, source, target, weight='weight') # type: ignore
        return path

    def get_all_shortest_paths(self, source: str, target: str) -> List[List[str]]:
        return list(nx.all_shortest_paths(self._graph, source, target, weight='weight')) # type: ignore

    def get_subgraph(self, nodes: List[str]) -> _NetworkXHandler:
        return _NetworkXHandler(nx.subgraph(self._graph, nodes)) # type: ignore
    
    def get_path_graph(self, path: List[str]) -> _NetworkXHandler:
        return _NetworkXHandler(nx.path_graph(self._graph, path)) # type: ignore
    
    def get_all_edges(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Returns all edges with data as a list of tuples (source, target, data)"""
        return list(self._graph.edges(data=True))

    def get_edge_data(self, source: str, target: str) -> Dict[str, Any]:
        return self._graph.edges[source, target]
    
    def get_distance(self, source: str, target: str) -> float:
        return nx.shortest_path_length(self._graph, source, target, weight='weight') # type: ignore

    def draw(self) -> None:
        nx.draw(self._graph, with_labels=True) # type: ignore
        plt.show() # type: ignore

    def __getitem__(self, key: str) -> Dict[str, Any]:
        return self._graph.nodes[key]
    

class SystemGraph:

    def __init__(self) -> None:
        self._graph: _NetworkXHandler = _NetworkXHandler()
        self._equipment_map: Dict[str, Location] = {}

    @property
    def locations(self) -> Dict[str, Location]:
        nodes = {}
        for name, node in self._graph.get_nodes().items(): 
            nodes[name] = node["location"]
        return nodes
    
    def add_location(self, location: Location) -> None:
        self._graph.add_node(location.teachpoint_name, location=location)
        if isinstance(location.resource, LabwareLoadable):
            self._equipment_map[location.teachpoint_name] = location

    def get_resource_location(self, resource_name: str) -> Location:
        if resource_name not in self._equipment_map.keys():
            raise ValueError(f"Resource {resource_name} does not exist")
        return self._equipment_map[resource_name]

    def add_edge(self, start: str, end: str, transporter: TransporterResource, weight: float = 5.0) -> None:
        if start not in self._graph.get_nodes():
            raise ValueError(f"Node {start} does not exist")
        if end not in self._graph.get_nodes():
            raise ValueError(f"Node {end} does not exist")
        self._graph.add_edge(start, end, transporter=transporter, weight=weight) 

    def set_edge_weight(self, start: str, end: str, weight: float) -> None:
        self._graph[start][end]['weight'] = weight

    def has_available_route(self, source: str, target: str) -> bool:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return available_graph.has_path(source, target)
    
    def has_any_route(self, source: str, target: str) -> bool:
        return self._graph.has_path(source, target)
    
    def get_blocking_locations(self, source: str, target: str) -> List[Location]:
        # TODO: add input validations of source and target entered
        blocking_locs: List[Location] = []
        for location_name in self.get_shortest_any_path(source, target):
            location: Location = self._graph.get_node_data(location_name)["location"]
            if not location.is_available:
                blocking_locs.append(location)
        return blocking_locs
    
    def get_all_blocking_locations(self, source: str, target: str) -> List[Location]:
        blocking_locs: List[Location] = []
        for path in self.get_all_shortest_any_paths(source, target):
            for location_name in path:
                location: Location = self._graph.get_node_data(location_name)["location"]
                if not location.is_available:
                    blocking_locs.append(location)
        return blocking_locs
    
    def get_shortest_available_path(self, source: str, target: str) -> List[str]:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return available_graph.get_shortest_path(source, target)
    
    def get_shortest_any_path(self, source: str, target: str) -> List[str]:
        return self._graph.get_shortest_path(source, target)
    
    def get_all_shortest_available_paths(self, source: str, target: str) -> List[List[str]]:
        available_graph = self._graph.get_subgraph([name for name, _ in self._get_available_locations().items()])
        return available_graph.get_all_shortest_paths(source, target)
    
    def get_all_shortest_any_paths(self, source: str, target: str) -> List[List[str]]:
        return self._graph.get_all_shortest_paths(source, target)
    
    def get_distance(self, source: str, target: str) -> float:
        return self._graph.get_distance(source, target)
    
    def get_transporter_between(self, source: str, target: str) -> TransporterResource:
        return self._graph.get_edge_data(source, target)["transporter"]
    
    def draw(self) -> None:
        self._graph.draw()
        

    def _get_available_locations(self) -> Dict[str, Dict[str, Any]]:
        nodes = {}
        for node, nodedata in self._graph.get_nodes().items():
            location: Location = nodedata["location"]
            if location.is_available:
                nodes[node] = nodedata
        return nodes
    



