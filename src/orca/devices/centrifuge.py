from typing import Optional

from orca.driver_management.drivers.driver_interfaces import ICentrifugeDriver
from orca.driver_management.drivers.plr_wrappers import PLRCentrifugeBackendWrapper
from orca.driver_management.drivers.sims import SimCentrifugeDriver
from orca.resource_models.devices import Device, DooredDeviceMixin, InitializableDeviceMixin
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager
from pylabrobot.centrifuge.backend import CentrifugeBackend as PLRCentrifugeBackend

class Centrifuge(DooredDeviceMixin):
    def __init__(self, 
                 name: str, 
                 driver: ICentrifugeDriver | PLRCentrifugeBackend,
                 sim: bool = False,
                 sim_driver: Optional[ICentrifugeDriver] = None
                 ):
        self._name = name
        driver = PLRCentrifugeBackendWrapper(driver) if isinstance(driver, PLRCentrifugeBackend) else driver
        sim_driver = sim_driver if sim_driver else SimCentrifugeDriver("centrifuge")

        super().__init__(name, 
            driver,
            sim_driver,
            sim)

    async def centrifuge(self, speed: int, duration: int) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        async with self._lock:
            await self._driver.centrifuge(speed, duration)

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