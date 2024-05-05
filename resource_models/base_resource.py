from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

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
    
    @property
    def is_initialized(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        raise NotImplementedError

class BaseResource(IInitializableResource, ABC):

    def __init__(self, name: str):
        self._name = name
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name
    
    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    def __str__(self) -> str:
        return self._name

class LabwarePlaceable(ABC):
    @property
    def name(self) -> str:
        raise NotImplementedError
    
    @property
    def labware(self) -> Optional[Labware]:
        raise NotImplementedError
    
    def initialize_labware(self, labware: Labware) -> None:
        # TODO: Make async in future
        # TODO: this will need to be restricted to only initilaizing the labware, probably with a LabwareManager service
        raise NotImplementedError
    
    @abstractmethod
    async def prepare_for_pick(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def prepare_for_place(self, labware: Labware) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def notify_placed(self, labware: Labware) -> None:
        raise NotImplementedError

class Equipment(BaseResource, LabwarePlaceable, ABC):

    def __init__(self, name: str):
        super().__init__(name)
        self._command: Optional[str] = None
        self._options: Dict[str, Any] = {}

    def set_command(self, command: str) -> None:
       
        self._command = command

    def set_command_options(self, options: Dict[str, Any]) -> None:
       
        self._options = options

    @abstractmethod
    async def execute(self) -> None:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def loaded_labware(self) -> List[Labware]:
        raise NotImplementedError

    
    