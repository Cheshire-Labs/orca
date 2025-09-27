from orca.devices.device_interfaces import ISealer
from orca.driver_management.drivers.driver_interfaces import ISealerDriver
from orca.driver_management.drivers.plr_wrappers import PLRSealerBackendWrapper
from orca.driver_management.drivers.sims import SimSealerDriver
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager
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

    @property
    def driver(self) -> ISealerDriver:
        return self._sim_manager.driver

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
        await self.driver.initialize(**self._init_options)  # type: ignore
        self._is_initialized = True

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