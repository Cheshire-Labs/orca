from typing import Optional
from orca.driver_management.drivers.driver_interfaces import IDelidderDriver, ILiquidHandlerDriver, IPlateWasherDriver, IStorageDriver, IWasteDriver
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager


class PlateWasher(Device):
    def __init__(self, 
                 name: str, 
                 driver: IPlateWasherDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IPlateWasherDriver] = None) -> None:
        sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim
            )
        super().__init__(name, sim_manager)

    @property
    def is_initialized(self) -> bool:
        """Returns whether the device is initialized or not."""
        return self._is_initialized
    
    async def initialize(self) -> None:
        """Initializes the device."""
        await self._driver.initialize()
        self._is_initialized = True
    
    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        """Prepare the device for picking up the specified labware."""
        await self._driver.open()
    
    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        """Prepare the device for picking up the specified labware."""
        await self._driver.open()
    
    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        """Notify the device that the specified labware has been picked."""
        await self._driver.close()
    
    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        """Notify the device that the specified labware has been placed."""
        await self._driver.close()


class LiquidHandler(Device):
    def __init__(self, 
                 name: str, 
                 driver: ILiquidHandlerDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[ILiquidHandlerDriver] = None) -> None:

        self._sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim
            )
        super().__init__(name, self._sim_manager)

class Delidder(Device):
    def __init__(self, 
                 name: str, 
                 driver: IDelidderDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IDelidderDriver] = None) -> None:
        self._sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim
            )
        super().__init__(name, self._sim_manager)


class Waste(Device):
    def __init__(self, 
                 name: str, 
                 driver: IWasteDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IWasteDriver] = None) -> None:

        self._sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim
            )
        super().__init__(name, self._sim_manager)

class Storage(Device):
    def __init__(self, 
                 name: str, 
                 driver: IStorageDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IStorageDriver] = None) -> None:
        self._sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim
            )
        super().__init__(name, self._sim_manager)

class Reader(Device):
    def __init__(self, 
                 name: str, 
                 driver: IStorageDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IStorageDriver] = None) -> None:
        self._sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim
            )
        super().__init__(name, self._sim_manager)