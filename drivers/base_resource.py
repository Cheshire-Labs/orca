from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    
    @abstractmethod
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        raise NotImplementedError

class IExecutableResource(IResource, ABC):
    @abstractmethod
    def initialize(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def is_initialized(self) -> bool:
        raise NotImplementedError
    
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

class ILabwareableResource(IResource, ABC):
    @property
    @abstractmethod
    def plate_pad(self) -> str:
        raise NotImplementedError
    

class BaseEquipmentResource(ILabwareableResource, IExecutableResource, ABC):

    def __init__(self, name: str):
        self._name = name
        self._is_running = False
        self._command: Optional[str] = None
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}
        self._options: Dict[str, Any] = {}
        self._plate_pad: str = name.replace(" ", "_").replace("-", "_")

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def plate_pad(self) -> str:
        return self._plate_pad
    
    @plate_pad.setter
    def plate_pad(self, plate_pad: str) -> None:
        self._plate_pad = plate_pad

    def is_initialized(self) -> bool:
        return self._is_initialized

    def is_running(self) -> bool:
        return self._is_running
    
    def set_command(self, command: str) -> None:
        self._command = command.upper()

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options
        if "plate-pad" in init_options.keys():
            self._plate_pad = init_options["plate-pad"]

    def set_command_options(self, options: Dict[str, Any]) -> None:
        self._options = options
    
    @abstractmethod
    def initialize(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def execute(self) -> None:
        raise NotImplementedError