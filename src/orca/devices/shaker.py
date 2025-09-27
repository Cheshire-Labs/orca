from typing import Optional, Union
from orca.devices.device_interfaces import IShaker
from orca.driver_management.drivers.driver_interfaces import IShakerDriver
from orca.driver_management.drivers.plr_wrappers import PLRShakerBackendWrapper
from orca.driver_management.drivers.sims import SimShakerDriver
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager
from pylabrobot.shaking.backend import ShakerBackend as PLRShakerBackend


import asyncio


class Shaker(Device, IShaker):
    """Driver for a shaker device."""

    def __init__(self, 
                 name: str, 
                 driver: Union[IShakerDriver, PLRShakerBackend], 
                 sim: bool = False,
                 sim_driver: Optional[IShakerDriver] = None):
        self._name = name
        driver = PLRShakerBackendWrapper(driver) if isinstance(driver, PLRShakerBackend) else driver
        sim_driver = sim_driver if sim_driver else SimShakerDriver(name)
        self._simulation_manager = SimulationManager(
            driver,
            sim_driver,
            sim
        )

    @property
    def driver(self) -> IShakerDriver:
        """Get the shaker backend."""
        return self._simulation_manager.driver

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    async def initialize(self) -> None:
        """
        Initialize the driver.
        """
        async with self._lock:
            await self.driver.setup(**self._init_options)  # type: ignore
            self._is_initialized = True

    @property
    def is_running(self) -> bool:
        """Check if the driver is running."""
        return self._lock.locked()

    async def shake(self, duration: int, speed: int) -> None:
        """Shake the shaker for a specified duration at a given speed."""
        await self.driver.shake(speed, duration)

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        await self.driver.stop_shaking()
        await self.driver.unlock_plate()

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        await self.driver.stop_shaking()
        await self.driver.unlock_plate()

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        pass

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        pass