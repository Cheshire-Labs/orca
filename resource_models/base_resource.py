from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from resource_models.labware import Labware

class ResourceUnavailableError(Exception):
    def __init__(self, message: str = "Resource is unavailable.") -> None:
        super().__init__(message)
    
    
class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    

class IInitializableResource(IResource, ABC):
    @abstractmethod
    def initialize(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def is_initialized(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        raise NotImplementedError

class BaseResource(IInitializableResource, ABC):

    def __init__(self, name: str):
        self._name = name
        self._is_busy = False
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}
        self._options: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    def is_initialized(self) -> bool:
        return self._is_initialized

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options
    
    def __str__(self) -> str:
        return self._name

class LabwareLoadable(ABC):
    @property
    def labware(self) -> Optional[Labware]:
        raise NotImplementedError
    
    @abstractmethod
    def load_labware(self, labware: Labware) -> None:
        raise NotImplementedError

    @abstractmethod
    def unload_labware(self, labware: Labware) -> None:
        raise NotImplementedError
    
class BaseLabwareableResource(BaseResource, LabwareLoadable):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._labware: Optional[Labware] = None

    @property
    def is_busy(self) -> bool:
        return self._labware is not None
    
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



    

    
    