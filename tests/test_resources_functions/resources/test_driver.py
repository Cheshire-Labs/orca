from typing import Any, Dict
from orca_driver_interface.driver_interfaces import IDriver


class TestDriverDriver(IDriver):
    def __init__(self) -> None:
        self._name = "Test Driver"

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return False

    @property
    def is_running(self) -> bool:
        return False

    @property
    def is_connected(self) -> bool:
        return False

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        pass

    async def initialize(self) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        print(f"Executing command: {command} with options: {options}")
