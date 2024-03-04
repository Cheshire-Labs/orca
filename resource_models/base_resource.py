from abc import ABC, abstractmethod
from typing import Any, Dict, Optional




class ResourceUnavailableError(Exception):
    def __init__(self, message: str = "Resource is unavailable.") -> None:
        super().__init__(message)
    
    
class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    
class IUseable(IResource, ABC):
    @property
    @abstractmethod
    def is_busy(self) -> bool:
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

class IExecutable(IInitializableResource, IUseable, ABC):
    
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def set_command(self, command: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_command_options(self, options: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def execute(self) -> None:
        raise NotImplementedError


class BaseEquipmentResource(IInitializableResource, IUseable, ABC):

    def __init__(self, name: str):
        self._name = name
        self._is_running = False
        self._command: Optional[str] = None
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}
        self._options: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_busy(self) -> bool:
        return self._is_running

    def is_initialized(self) -> bool:
        return self._is_initialized

    def is_running(self) -> bool:
        return self._is_running

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options



    

    
    