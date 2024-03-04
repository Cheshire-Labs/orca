from abc import ABC, abstractmethod
from typing import Optional
from resource_models.base_resource import IExecutable, ResourceUnavailableError
from resource_models.base_resource import BaseEquipmentResource
from resource_models.labware import Labware


class ILabwareLoadable(ABC):
    
    @abstractmethod
    def load_labware(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def unload_labware(self, labware: Labware) -> None:
        raise NotImplementedError
    
class LoadableEquipmentResource(BaseEquipmentResource, IExecutable, ILabwareLoadable):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._labware: Optional[Labware] = None

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware
    
    def load_labware(self, labware: Labware) -> None:
        if self._labware is not None:
            raise ResourceUnavailableError(f"Error setting labware '{labware}' to resource '{self}'.  Resource is already occupied by '{self._labware}'")
        self._labware = labware

    def unload_labware(self, labware: Labware) -> None:
        if self._labware != labware:
            raise ResourceUnavailableError(f"Error unloading labware '{labware}' from resource '{self}'.  Resource does not contain '{labware}' resource is occupied by '{self._labware}'")
        self._labware = None