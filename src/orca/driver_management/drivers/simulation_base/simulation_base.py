import logging
import time
from typing import Any, Dict, Optional

from orca_driver_interface.driver_interfaces import IDriver

orca_logger = logging.getLogger("orca")

class SimulationBaseDriver(IDriver):
    def __init__(self, name: str, mocking_type: Optional[str] = None, sim_time: float = 0.2):
        self._name: str = name
        self._mocking_type = mocking_type
        self._init_options: Dict[str, Any] = {}
        self._is_initialized: bool = False
        self._is_running: bool = False
        self._connected: bool = False
        self._sim_time = sim_time

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        orca_logger.info(f"{self._name} executing command: {command}")
        if len(options.keys()) > 0:
            orca_logger.info(f"Options: {options}")
        self._sleep()
        orca_logger.info(f"{self._name} executed command: {command}")

    def _sleep(self) -> None:
        self._is_running = True
        time.sleep(self._sim_time)
        self._is_running = False