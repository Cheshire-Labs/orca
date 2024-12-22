from typing import Any, Dict, Optional
from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver

class NullPlatePadDriver(ILabwarePlaceableDriver):
    def __init__(self, name: str) -> None:
        self._name = name
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def is_running(self) -> bool:
        return False

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        pass

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    async def initialize(self) -> None:
        self._is_initialized = True

    @property
    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        pass

    def __str__(self) -> str:
        return f"PlatePad: {self._name}"