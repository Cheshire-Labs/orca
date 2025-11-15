from typing import Optional, Dict, Any, Callable, List
import logging
from unittest.mock import MagicMock
from orca.driver_management.drivers.sims import (
    SimDriver, SimTransporterDriver, BaseSimDriver,
    ShakerSimMixin, CentrifugeSimMixin, ReaderSimMixin,
    DelidderSimMixin, ProtocolRunnerSimMixin
)
from orca.resource_models.transporter import Transporter
from orca.resource_models.devices import Device
from orca.resource_models.location import Location
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from orca.devices.device_interfaces import (
    IGenericExecutable, IProtocolRunner, ISealer,
    IShaker, ICentrifuge, IReader, IDelidder
)

orca_logger = logging.getLogger("orca")

class MockEquipmentResource(Device):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        driver = SimDriver(name, mocking_type)
        super().__init__(name, driver, driver, sim=True)
        self._on_intialize: Callable[[], None] = lambda: None
        self._on_prepare_for_place: Callable[[LabwareInstance], None] = lambda x: None
        self._on_prepare_for_pick: Callable[[LabwareInstance], None] = lambda x: None
        self._on_notify_picked: Callable[[LabwareInstance], None] = lambda x: None
        self._on_notify_placed: Callable[[LabwareInstance], None] = lambda x: None
        self._on_execute: Callable[[str], None] = lambda x: None

    async def initialize(self) -> None:
        await super().initialize()
        self._on_intialize()

    async def prepare_for_place(self, labware: LabwareInstance) -> None:
        await super().prepare_for_place(labware)
        self._on_prepare_for_place(labware)
        
    async def prepare_for_pick(self, labware: LabwareInstance) -> None:
        await super().prepare_for_pick(labware)
        self._on_prepare_for_pick(labware)
        
    async def notify_picked(self, labware: LabwareInstance) -> None:
        await super().notify_picked(labware)
        self._on_notify_picked(labware)
            
    async def notify_placed(self, labware: LabwareInstance) -> None:
        await super().notify_placed(labware)
        self._on_notify_placed(labware)

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        await super().execute(command, options)
        self._on_execute(command)


class MockRoboticArm(Transporter):
    def __init__(self, name: str, mocking_type: Optional[str] = None, positions: Optional[List[str]] = None) -> None:
        driver = SimTransporterDriver(name)
        positions = positions if positions is not None else []
        teachpoints = [Teachpoint(pos, 0.0, 0.0, 0.0) for pos in positions]
        super().__init__(name, driver, load_positions=teachpoints, sim=True)
        self._on_pick: Callable[[LabwareInstance, Location], None] = lambda x, y: None
        self._on_place: Callable[[LabwareInstance, Location], None] = lambda x, y: None

    async def pick(self, location: Location) -> None:
        await super().pick(location)
        if self._labware is None: 
            raise ValueError(f"{self} does not contain labware.  Base class pick method did not raise error")
        self._on_pick(self._labware, location)
        
    
    async def place(self, location: Location) -> None:
        if self._labware is None:
            raise ValueError(f"{self} does not contain labware to place")
        labware = self._labware
        await super().place(location)
        self._on_place(labware, location)


class UniversalSimDriver(BaseSimDriver, ShakerSimMixin, CentrifugeSimMixin, ReaderSimMixin, DelidderSimMixin, ProtocolRunnerSimMixin):
    """Universal simulation driver for testing - supports ALL action interfaces"""

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        """Execute a generic command"""
        await self._sim(f"Executing command: {command} with options: {options}")
        orca_logger.info(f"Command {command} executed successfully.")

    async def open(self) -> None:
        """Override to use BaseSimDriver's version with self.name"""
        await BaseSimDriver.open(self)

    async def close(self) -> None:
        """Override to use BaseSimDriver's version with self.name"""
        await BaseSimDriver.close(self)


class UniversalMockDevice(Device, IGenericExecutable, IProtocolRunner, ISealer, IShaker, ICentrifuge, IReader, IDelidder):
    """Universal mock device for testing - supports ALL action types"""

    def __init__(self, name: str):
        driver = UniversalSimDriver(name)
        super().__init__(name, driver, driver, sim=True)

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        await self.driver.execute(command, options)

    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        await self.driver.run_protocol(protocol_filepath, params)

    async def seal(self, temperature: int, duration: float) -> None:
        await self.driver.seal(temperature, duration)

    async def shake(self, duration: int, speed: int) -> None:
        await self.driver.shake(speed, duration)

    async def centrifuge(self, speed: int, duration: int) -> None:
        await self.driver.centrifuge(speed, duration)

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        await self.driver.read(protocol_filepath, output_filepath)

    async def delid(self) -> None:
        await self.driver.delid()


