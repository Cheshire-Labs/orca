from abc import ABC
from typing import Any, Dict

class IResource(ABC):
    def is_running(self) -> bool:
        raise NotImplementedError
    def initialize(self) -> bool:
        raise NotImplementedError

    def is_initialized(self) -> bool:
        raise NotImplementedError

    def set_options(self, options: Dict[str, Any]) -> None:
        raise NotImplementedError

    def execute(self) -> None:
        raise NotImplementedError

    def load_plate(self) -> None:
        raise NotImplementedError

    def unload_plate(self) -> None:
        raise NotImplementedError
    

class BaseResource(IResource):
    def __init__(self, config: Dict[str, Any]):
        self._ip = config['ip'] or 'localhost'
        self._port = config['port'] or '3221'
        self._is_running = False
        self._is_initialized = False
        self._options = {}
        self._protocol = None
    def is_initialized(self) -> bool:
        return self._is_initialized

    def is_running(self) -> bool:
        return self._is_running

    def set_options(self, options: Dict[str, Any]) -> None:
        self._options = options