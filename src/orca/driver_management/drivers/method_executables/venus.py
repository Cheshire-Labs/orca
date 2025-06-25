import typing
from typing import Any, Dict, Optional

from orca.driver_management.driver_interfaces import IProtocolRunner
from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver


if typing.TYPE_CHECKING:
    from orca_driver_venus.venus_driver import VenusProtocolDriver as _VenusProtocolDriver

class VenusProtocolDriver(ILabwarePlaceableDriver, IProtocolRunner):
    """
    Driver for interfacing with Hamilton Venus protocols.
    """

    def __init__(self, name: str):
        try:
            from orca_driver_venus.venus_driver import VenusProtocolDriver as _VenusProtocolDriver
        except ImportError:
            raise ImportError("Orca Venus driver is not installed. Please install it to use VenusProtocolDriver.")
        self._driver: _VenusProtocolDriver = _VenusProtocolDriver(name)

    @property
    def name(self) -> str:
        return self._driver.name
    
    @property
    def is_connected(self) -> bool:
        return self._driver.is_connected

    @property
    def is_initialized(self) -> bool:
        return self._driver.is_initialized

    async def connect(self) -> None:
        await self._driver.connect()

    async def disconnect(self) -> None:
        await self._driver.disconnect()

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._driver.set_init_options(init_options)

    async def initialize(self) -> None:
        await self._driver.initialize()

    @property
    def is_running(self) -> bool:
        return self._driver.is_running

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        await self._driver.prepare_for_place(labware_name, labware_type, barcode, alias)

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        await self._driver.prepare_for_pick(labware_name, labware_type, barcode, alias)

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        await self._driver.notify_picked(labware_name, labware_type, barcode, alias)

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        await self._driver.notify_placed(labware_name, labware_type, barcode, alias)

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        await self._driver.execute(command, options)

    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        return await self.execute(protocol_filepath, params) # TODO: switch this to driver protocol once driver is updated