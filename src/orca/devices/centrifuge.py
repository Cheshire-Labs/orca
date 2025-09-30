from typing import Optional

from orca.devices.device_interfaces import ICentrifuge
from orca.driver_management.drivers.driver_interfaces import ICentrifugeDriver
from orca.driver_management.drivers.plr_wrappers import PLRCentrifugeBackendWrapper
from orca.driver_management.drivers.sims import SimCentrifugeDriver
from orca.resource_models.devices import Device
from pylabrobot.centrifuge.backend import CentrifugeBackend as PLRCentrifugeBackend

class Centrifuge(Device[ICentrifugeDriver], ICentrifuge):
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
        await self.driver.centrifuge(speed, duration)
