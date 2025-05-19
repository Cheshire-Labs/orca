from typing import Optional, Dict, Any, Callable, List
from unittest.mock import MagicMock
from orca.driver_management.drivers.simulation_labware_placeable.simulation_labware_placeable import SimulationDeviceDriver
from orca.driver_management.drivers.simulation_robotic_arm.simulation_robotic_arm import LegacySimulationRoboticArmDriver
from orca.helper import FilepathReconciler
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.resource_models.base_resource import Device
from orca.resource_models.location import Location
from orca.resource_models.labware import Labware


class MockEquipmentResource(Device):
    def __init__(self, name: str, mocking_type: Optional[str] = None):
        super().__init__(name, SimulationDeviceDriver(name, mocking_type))
        self._on_intialize: Callable[[], None] = lambda: None
        self._on_prepare_for_place: Callable[[Labware], None] = lambda x: None
        self._on_prepare_for_pick: Callable[[Labware], None] = lambda x: None
        self._on_notify_picked: Callable[[Labware], None] = lambda x: None
        self._on_notify_placed: Callable[[Labware], None] = lambda x: None
        self._on_execute: Callable[[str], None] = lambda x: None

    async def initialize(self) -> None:
        await super().initialize()
        self._on_intialize()

    async def prepare_for_place(self, labware: Labware) -> None:
        await super().prepare_for_place(labware)
        self._on_prepare_for_place(labware)
        
    async def prepare_for_pick(self, labware: Labware) -> None:
        await super().prepare_for_pick(labware)
        self._on_prepare_for_pick(labware)
        
    async def notify_picked(self, labware: Labware) -> None:
        await super().notify_picked(labware)
        self._on_notify_picked(labware)
            
    async def notify_placed(self, labware: Labware) -> None:
        await super().notify_placed(labware)
        self._on_notify_placed(labware)

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        await super().execute(command, options)
        self._on_execute(command)


class MockRoboticArm(TransporterEquipment):
    def __init__(self, name: str, mocking_type: Optional[str] = None, positions: List[str] = []) -> None:
        file_reconciler = MagicMock(FilepathReconciler)
        driver = LegacySimulationRoboticArmDriver(name, file_reconciler, mocking_type)
        driver.set_init_options({"positions": positions})
        super().__init__(name, driver)
        self._on_pick: Callable[[Labware, Location], None] = lambda x, y: None
        self._on_place: Callable[[Labware, Location], None] = lambda x, y: None

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

