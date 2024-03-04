from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from resource_models.loadable_resources.location import Location
from resource_models.loadable_resources.location import Location
import networkx as nx


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
    
    

class SystemGraph:

    def __init__(self) -> None:
        self._graph: _NetworkXHandler = _NetworkXHandler()
        self._equipment_map: Dict[str, str] = {}

    @property
    def locations(self) -> Dict[str, Location]:
        nodes = {}
        for name, node in self._graph.get_nodes().items(): 
            nodes[name] = node["location"]
        return nodes
    
    def add_location(self, location: Location) -> None:
        self._graph.add_node(location.name, location)
        if location.resource is not None:
            self._equipment_map[location.resource.name] = location.name

    def get_resource_location(self, resource_name: str) -> str:
        if resource_name not in self._equipment_map.keys():
            raise ValueError(f"Resource {resource_name} does not exist")
        return self._equipment_map[resource_name]

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
    
    # def get_shortest_any_route(self, source: str, target: str) -> Route:
    #     path: List[str] = self._get_shortest_any_path(source, target)
    #     return self._get_route_from_path(path)
    
    # def get_all_shortest_any_routes(self, source: str, target: str) -> List[Route]:
    #     paths: List[List[str]] = self._get_all_shortest_any_paths(source, target)
    #     return [self._get_route_from_path(path) for path in paths]
    
    # def get_shortest_available_route(self, source: str, target: str) -> Route:
    #     path: List[str] = self._get_shortest_available_path(source, target)
    #     return self._get_route_from_path(path)
    
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
    
    # def get_all_shortest_available_routes(self, source: str, target: str) -> List[Route]:
    #     paths: List[List[str]] = self._get_all_shortest_available_paths(source, target)
    #     return [self._get_route_from_path(path) for path in paths]
    
    
    
    def get_blocking_locations(self, source: str, target: str) -> List[Location]:
        # TODO: add input validations of source and target entered
        locations: List[Location] = []
        for location_name in self.get_shortest_any_path(source, target):
            location: Location = self._graph.get_node_data(location_name)["location"]
            if location.in_use:
                locations.append(location)
        return locations
    
    def get_all_blocking_locations(self, source: str, target: str) -> List[Location]:
        locations: List[Location] = []
        for path in self.get_all_shortest_any_paths(source, target):
            for location_name in path:
                location: Location = self._graph.get_node_data(location_name)["location"]
                if location.in_use:
                    locations.append(location)
        return locations
    
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
    
    def _get_available_locations(self) -> Dict[str, Dict[str, Any]]:
        nodes = {}
        for node, nodedata in self._graph.get_nodes().items():
            location: Location = nodedata["location"]
            if not location.in_use:
                nodes[node] = nodedata
        return nodes
    
    # def _get_route_from_path(self, path: List[str]) -> Route:
    #     path_graph = self._graph.get_path_graph(path)
    #     steps: List[RouteSingleStep] = []
    #     for start_name, end_name, data in path_graph.get_all_edges():
    #         start = self._graph.get_node_data(start_name)["location"]
    #         end = self._graph.get_node_data(end_name)["location"]
    #         steps.append(RouteSingleStep(start, end, data["weight"], data["action"]))
    #     return Route(steps)



