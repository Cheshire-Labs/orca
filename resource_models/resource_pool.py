from typing import Any, List
from resource_models.base_resource import Equipment
from resource_models.location import Location
from routing.system_graph import SystemGraph
    
class EquipmentResourcePool:

    def __init__(self, name: str, resources: List[Equipment] = []):
        self._name = name
        self._resources: List[Equipment] = resources

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def resources(self) -> List[Equipment]:
        return self._resources

    def add_resource(self, resource: Equipment) -> None:
        self._resources.append(resource)

    def get_closest_available_resource(self, reference_point: Location, system_graph: SystemGraph) -> Equipment:
        available_resources = [resource for resource in self.resources if resource.is_available]
        ordered_resources = sorted(available_resources, key=lambda x: 
                                   system_graph.get_distance(reference_point.teachpoint_name, 
                                                       system_graph.get_resource_location(x.name).teachpoint_name))
        if not ordered_resources:
            raise ValueError(f"{self}: No available resources")
        return ordered_resources[0]