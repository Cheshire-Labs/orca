from typing import List, Optional, Sequence
from orca.resource_models.resources import IResource


class ResourcePool:

    def __init__(self, name: str, resources: Optional[Sequence[IResource]] = None):
        self._name = name
        self._resources: List[IResource] = list(resources) if resources is not None else []

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def resources(self) -> List[IResource]:
        return self._resources

    def add_resource(self, resource: IResource) -> None:
        self._resources.append(resource)
