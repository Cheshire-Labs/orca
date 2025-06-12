from typing import List, Optional
from orca.resource_models.base_resource import Equipment
from orca.resource_models.location import Location

    
class EquipmentResourcePool:

    def __init__(self, name: str, resources: Optional[List[Equipment]] = None):
        self._name = name
        self._resources: List[Equipment] = resources if resources is not None else []

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def resources(self) -> List[Equipment]:
        return self._resources

    def add_resource(self, resource: Equipment) -> None:
        self._resources.append(resource)
