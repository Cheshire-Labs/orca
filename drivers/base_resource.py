from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

class IResource(ABC):
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def initialize(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def is_initialized(self) -> bool:
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

class BaseResource(IResource, ABC):
    def __init__(self, name: str):
        self._name = name
        self._is_running = False
        self._command: Optional[str] = None
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}
        self._options: Dict[str, Any] = {}

    @abstractmethod
    def initialize(self) -> bool:
        raise NotImplementedError
    
    def is_initialized(self) -> bool:
        return self._is_initialized

    def is_running(self) -> bool:
        return self._is_running
    
    def set_command(self, command: str) -> None:
        self._command = command.upper()

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    def set_command_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    @abstractmethod
    def execute(self) -> None:
        raise NotImplementedError