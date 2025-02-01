from abc import ABC, abstractmethod
import asyncio
from orca.resource_models.base_resource import Equipment, IInitializableResource, IResource
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.resource_models.transporter_resource import TransporterEquipment


from typing import Dict, List

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

    @property
    @abstractmethod
    def equipments(self) -> List[Equipment]:
        raise NotImplementedError

    @abstractmethod
    def get_equipment(self, name: str) -> Equipment:
        raise NotImplementedError

    @property
    @abstractmethod
    def transporters(self) -> List[TransporterEquipment]:
        raise NotImplementedError

    @abstractmethod
    def get_transporter(self, name: str) -> TransporterEquipment:
        raise NotImplementedError

    @property
    @abstractmethod
    def resource_pools(self) -> List[EquipmentResourcePool]:
        raise NotImplementedError

    @abstractmethod
    def get_resource_pool(self, name: str) -> EquipmentResourcePool:
        raise NotImplementedError

    @abstractmethod
    def add_resource_pool(self, resource_pool: EquipmentResourcePool) -> None:
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
    def __init__(self) -> None:
        self._resources: Dict[str, IResource] = {}
        self._resource_pools: Dict[str, EquipmentResourcePool] = {}
        self._observers: List[IResourceRegistryObesrver] = []

    @property
    def resources(self) -> List[IResource]:
        return list(self._resources.values())

    @property
    def equipments(self) -> List[Equipment]:
        return [r for r in self._resources.values() if isinstance(r, Equipment)]

    @property
    def transporters(self) -> List[TransporterEquipment]:
        return [r for r in self._resources.values() if isinstance(r, TransporterEquipment)]

    @property
    def resource_pools(self) -> List[EquipmentResourcePool]:
        return list(self._resource_pools.values())

    def get_resource(self, name: str) -> IResource:
        return self._resources[name]

    def get_equipment(self, name: str) -> Equipment:
        resource = self.get_resource(name)
        if not isinstance(resource, Equipment):
            raise ValueError(f"Resource {name} is not an Equipment resource")
        return resource

    def get_transporter(self, name: str) -> TransporterEquipment:
        resource = self.get_resource(name)
        if not isinstance(resource, TransporterEquipment):
            raise ValueError(f"Resource {name} is not an Equipment resource")
        return resource

    def add_resource(self, resource: IResource) -> None:
        name = resource.name
        if name in self._resources.keys():
            raise KeyError(f"Resource {name} is already defined in the system.  Each resource must have a unique name")
        self._resources[name] = resource
        [observer.resource_registry_notify("resource_added", resource) for observer in self._observers]

    def get_resource_pool(self, name: str) -> EquipmentResourcePool:
        return self._resource_pools[name]

    def add_resource_pool(self, resource_pool: EquipmentResourcePool) -> None:
        name = resource_pool.name
        if name in self._resource_pools.keys():
            raise KeyError(f"Resource Pool {name} is already defined in the system.  Each resource pool must have a unique name")
        self._resource_pools[name] = resource_pool

    def add_observer(self, observer: IResourceRegistryObesrver) -> None:
        self._observers.append(observer)

    async def initialize_all(self) -> None:
        await asyncio.gather(*[r.initialize() for r in self._resources.values() if isinstance(r, IInitializableResource)])

    def clear_resources(self) -> None:
        self._resources.clear()
        self._resource_pools.clear()
        self._observers.clear()