from abc import ABC, abstractmethod
from resource_models.labware import Labware


class ILabwareLoadable(ABC):
    
    @abstractmethod
    def load_labware(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def unload_labware(self, labware: Labware) -> None:
        raise NotImplementedError