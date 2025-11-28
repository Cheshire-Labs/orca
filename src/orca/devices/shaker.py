from typing import Optional, Union
from orca.devices.device_interfaces import IShaker
from cheshire_drivers import IShakerDriver, PLRShakerBackendWrapper, SimShakerDriver
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


    async def shake(self, duration: int, speed: int) -> None:
        """Shake the shaker for a specified duration at a given speed."""
        await self.driver.shake(speed, duration)