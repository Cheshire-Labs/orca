from __future__ import annotations
from abc import ABC, abstractmethod
import itertools
from typing import Any, Dict, List, Optional, Tuple
from resource_models.base_resource import IResource, LabwareLoadable
from resource_models.location import ILocationObeserver, Location
import networkx as nx
import matplotlib.pyplot as plt

from resource_models.transporter_resource import TransporterResource
from system.resource_registry import IResourceRegistry
from system.resource_registry import IResourceRegistryObesrver



class IResourceLocator(ABC):
    def get_resource_location(self, resource_name: str) -> Location:
        raise NotImplementedError
    
class IRouteBuilder(ABC):
    
        def get_shortest_available_path(self, source: str, target: str) -> List[str]:
            raise NotImplementedError
        
        def get_shortest_any_path(self, source: str, target: str) -> List[str]:
            raise NotImplementedError
        
        def get_all_shortest_available_paths(self, source: str, target: str) -> List[List[str]]:
            raise NotImplementedError
        
        def get_all_shortest_any_paths(self, source: str, target: str) -> List[List[str]]:
            raise NotImplementedError
        
        def get_distance(self, source: str, target: str) -> float:
            raise NotImplementedError
        
        def get_transporter_between(self, source: str, target: str) -> TransporterResource:
            raise NotImplementedError
        
        def has_available_route(self, source: str, target: str) -> bool:
            raise NotImplementedError
        
        def has_any_route(self, source: str, target: str) -> bool:
            raise NotImplementedError
        
        def get_blocking_locations(self, source: str, target: str) -> List[Location]:
            raise NotImplementedError
        
        def get_all_blocking_locations(self, source: str, target: str) -> List[Location]:
            raise NotImplementedError
        
        def draw(self) -> None:
            raise NotImplementedError

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


class ILocationRegistry(ABC):
    @abstractmethod
    def get_location(self, name: str) -> Location:
        raise NotImplementedError

    @abstractmethod
    def add_location(self, location: Location):
        raise NotImplementedError
    

class SystemMap(ILocationRegistry, IRouteBuilder, IResourceLocator, ILocationObeserver, IResourceRegistryObesrver):

    def __init__(self, resource_registry: IResourceRegistry) -> None:
        self._graph: _NetworkXHandler = _NetworkXHandler()
        self._equipment_map: Dict[str, Location] = {}
        self._resource_registry = resource_registry
        for transporter in self._resource_registry.transporters:
            self.add_transporter(transporter)
        self._resource_registry.add_observer(self)

    def get_location(self, name: str) -> Location:
        return self._graph.get_node_data(name)["location"]

    def add_location(self, location: Location) -> None:
        self._graph.add_node(location.teachpoint_name, location=location)
        if isinstance(location.resource, LabwareLoadable):
            self._equipment_map[location.resource.name] = location
        location.add_observer(self)

    def get_resource_location(self, resource_name: str) -> Location:
        try:
            return self._equipment_map[resource_name]
        except KeyError:
            resource_name = resource_name.replace("-", "_")
            try:
                return self._equipment_map[resource_name]
            except KeyError:
                raise ValueError(f"Resource {resource_name} does not exist")

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

    def add_transporter(self, transporter: TransporterResource) -> None:
        taught_locations = transporter.get_taught_positions()
        # add teachpoints as locations if they don't exist and connect them as an edge
        for edge in itertools.combinations(taught_locations, 2):
            try:
                self.get_location(edge[0])
            except KeyError:
                self.add_location(Location(edge[0]))
            try:
                self.get_location(edge[1])
            except KeyError:
                self.add_location(Location(edge[1]))
            self.add_edge(edge[0], edge[1], transporter)
            self.add_edge(edge[1], edge[0], transporter)

    def resource_registry_notify(self, event: str, resource: IResource) -> None:
        if event == "resource_added":
            if isinstance(resource, TransporterResource):
                self.add_transporter(resource)

    def location_notify(self, event: str, location: Location, resource: IResource) -> None:
        if event == "resource_set":
            if isinstance(resource, LabwareLoadable):
                self._equipment_map[resource.name] = location
