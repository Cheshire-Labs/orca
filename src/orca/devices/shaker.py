from typing import Optional, Union
from orca.devices.device_interfaces import IShaker
from orca.driver_management.drivers.driver_interfaces import IShakerDriver
from orca.driver_management.drivers.plr_wrappers import PLRShakerBackendWrapper
from orca.driver_management.drivers.sims import SimShakerDriver
from orca.resource_models.devices import Device
from pylabrobot.shaking.backend import ShakerBackend as PLRShakerBackend



class Shaker(Device[IShakerDriver], IShaker):
    """Driver for a shaker device."""

    def __init__(self, 
                 name: str, 
                 driver: Union[IShakerDriver, PLRShakerBackend], 
                 sim: bool = False,
                 sim_driver: Optional[IShakerDriver] = None):
        driver = PLRShakerBackendWrapper(driver) if isinstance(driver, PLRShakerBackend) else driver
        _sim_driver: IShakerDriver = sim_driver or SimShakerDriver(name)
        super().__init__(name, driver, _sim_driver, sim)


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