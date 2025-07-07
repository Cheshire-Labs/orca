from abc import ABC, abstractmethod
import asyncio
from orca.resource_models.devices import Device
from orca.resource_models.resources import IInitializable, IResource
from orca.resource_models.resources import ISimulationable
from orca.resource_models.resource_pool import ResourcePool


from typing import Dict, List

from orca.resource_models.transporter import Transporter

class IResourceRegistryObesrver(ABC):
    @abstractmethod
    def resource_registry_notify(self, event: str, resource: IResource) -> None:
        raise NotImplementedError()

class IResourceRegistry(ABC):

    @property
    @abstractmethod
    def resources(self) -> List[IResource]:
        raise NotImplementedError

    @abstractmethod
    def get_resource(self, name: str) -> IResource:
        raise NotImplementedError

    @abstractmethod
    def add_resource(self, resource: IResource) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def set_simulating(self, simulating: bool) -> None:
        """Sets the simulation state of the system."""
        raise NotImplementedError

    @property
    @abstractmethod
    def devices(self) -> List[Device]:
        raise NotImplementedError

    @abstractmethod
    def get_device(self, name: str) -> Device:
        raise NotImplementedError

    @property
    @abstractmethod
    def transporters(self) -> List[Transporter]:
        raise NotImplementedError

    @abstractmethod
    def get_transporter(self, name: str) -> Transporter:
        raise NotImplementedError

    @property
    @abstractmethod
    def resource_pools(self) -> List[ResourcePool]:
        raise NotImplementedError

    @abstractmethod
    def get_resource_pool(self, name: str) -> ResourcePool:
        raise NotImplementedError

    @abstractmethod
    def add_resource_pool(self, resource_pool: ResourcePool) -> None:
        raise NotImplementedError

    @abstractmethod
    def add_observer(self, observer: IResourceRegistryObesrver) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def initialize_all(self) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def clear_resources(self) -> None:
        raise NotImplementedError


class ResourceRegistry(IResourceRegistry):
    """ A class that manages resources and resource pools in the system."""
    def __init__(self) -> None:
        self._resources: Dict[str, IResource] = {}
        self._resource_pools: Dict[str, ResourcePool] = {}
        self._observers: List[IResourceRegistryObesrver] = []

    @property
    def resources(self) -> List[IResource]:
        return list(self._resources.values())

    @property
    def devices(self) -> List[Device]:
        return [r for r in self._resources.values() if isinstance(r, Device)]

    @property
    def transporters(self) -> List[Transporter]:
        return [r for r in self._resources.values() if isinstance(r, Transporter)]

    @property
    def resource_pools(self) -> List[ResourcePool]:
        return list(self._resource_pools.values())

    def get_resource(self, name: str) -> IResource:
        return self._resources[name]

    def get_device(self, name: str) -> Device:
        resource = self.get_resource(name)
        if not isinstance(resource, Device):
            raise ValueError(f"Resource {name} is not an Equipment resource")
        return resource

    def get_transporter(self, name: str) -> Transporter:
        resource = self.get_resource(name)
        if not isinstance(resource, Transporter):
            raise ValueError(f"Resource {name} is not an Equipment resource")
        return resource

    def add_resource(self, resource: IResource) -> None:
        name = resource.name
        if name in self._resources.keys():
            raise KeyError(f"Resource {name} is already defined in the system.  Each resource must have a unique name")
        self._resources[name] = resource
        [observer.resource_registry_notify("resource_added", resource) for observer in self._observers]

    def add_resources(self, resources: List[IResource | ResourcePool]) -> None:
        """Adds a list of resources or resource pools to the registry."""
        for resource in resources:
            if isinstance(resource, ResourcePool):
                self.add_resource_pool(resource)
            else:
                self.add_resource(resource)

    def get_resource_pool(self, name: str) -> ResourcePool:
        return self._resource_pools[name]

    def add_resource_pool(self, resource_pool: ResourcePool) -> None:
        name = resource_pool.name
        if name in self._resource_pools.keys():
            raise KeyError(f"Resource Pool {name} is already defined in the system.  Each resource pool must have a unique name")
        self._resource_pools[name] = resource_pool

    def add_observer(self, observer: IResourceRegistryObesrver) -> None:
        self._observers.append(observer)

    def set_simulating(self, simulating: bool) -> None:
        """Sets the simulation state of the system."""
        for resource in self._resources.values():
            if isinstance(resource, ISimulationable):
                resource.set_simulating(simulating)

    async def initialize_all(self) -> None:
        await asyncio.gather(*[r.initialize() for r in self._resources.values() if isinstance(r, IInitializable)])

    def clear_resources(self) -> None:
        self._resources.clear()
        self._resource_pools.clear()
        self._observers.clear()