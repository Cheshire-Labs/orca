from orca.devices.device_interfaces import ISealer
from cheshire_drivers import ISealerDriver, PLRSealerBackendWrapper, SimSealerDriver
from orca.resource_models.devices import Device
from pylabrobot.sealing.backend import SealerBackend as PLRSealerBackend


from typing import Dict, Optional


class Sealer(Device[ISealerDriver], ISealer):
    def __init__(self, 
                 name: str, 
                 driver: ISealerDriver | PLRSealerBackend, 
                 sim: bool = False, 
                 sim_driver: Optional[ISealerDriver] = None) -> None:
        driver = PLRSealerBackendWrapper(driver) if isinstance(driver, PLRSealerBackend) else driver
        sim_driver = sim_driver if sim_driver else SimSealerDriver(name)
        super().__init__(name, driver, sim_driver, sim)

    async def execute(self, command: str, options: Dict[str, str]) -> None:
        if command == "seal":
            temperature = int(options.get("temperature", 0))
            duration = float(options.get("duration", 0.0))
            await self.seal(temperature=temperature, duration=duration)
        elif command == "open":
            await self.driver.open()
        elif command == "close":
            await self.driver.close()
        else:
            raise ValueError(f"Unknown command: {command}")

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        await self.driver.seal(temperature=temperature, duration=duration)