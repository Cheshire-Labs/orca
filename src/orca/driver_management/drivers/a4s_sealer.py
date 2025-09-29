import asyncio

import logging
from orca.devices.device_interfaces import ISealer, ITempGettable, ITempSettable
from orca.driver_management.drivers.driver_interfaces import ISealerDriver
from orca.driver_management.drivers.plr_wrappers import PLRSealerBackendWrapper
from orca.driver_management.drivers.sims import SimSealerDriver
from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from pylabrobot.sealing.a4s_backend import A4SBackend

orca_logger = logging.getLogger("orca")



class SimulationA4SBackend:
    """A simulation backend for the A4S sealer."""
    def __init__(self, port: str, timeout: int = 20, sim_time: float = 0.1):
        self._simulated_temperature = 25.0  # Default simulated temperature
        self._sim_time = sim_time

    async def setup(self) -> None:
        """Simulate the setup of the A4S sealer."""
        orca_logger.info("Simulating setup of A4S sealer")
        await asyncio.sleep(self._sim_time)

    async def open(self) -> None:
        """Simulate opening the sealer."""
        orca_logger.info("Simulating opening the sealer")
        await asyncio.sleep(self._sim_time)

    async def close(self) -> None:
        """Simulate closing the sealer."""
        orca_logger.info("Simulating closing the sealer")
        await asyncio.sleep(self._sim_time)

    async def seal(self, temperature: int, duration: float) -> None:
        """Simulate sealing by just waiting for the duration."""
        orca_logger.info(f"Simulating sealing at {temperature}°C for {duration} seconds")
        await asyncio.sleep(duration)

    async def set_temperature(self, temperature: float) -> None:
        """Set the simulated temperature."""
        orca_logger.info(f"Setting simulated temperature to {temperature}°C")
        self._simulated_temperature = temperature

    async def get_temperature(self) -> float:
        """Get the current simulated temperature."""
        orca_logger.info(f"Getting simulated temperature: {self._simulated_temperature}°C")
        return self._simulated_temperature


class A4SSealer(Device[ISealerDriver]):
    def __init__(
        self,
        name: str,
        port: str,
        timeout: int = 20,
        sim: bool = False
    ):
        self.a4s_driver = A4SBackend(port, timeout)
        self.a4s_sim_driver = SimulationA4SBackend(port, timeout)
        super().__init__(name, 
                         PLRSealerBackendWrapper(self.a4s_driver),
                        SimSealerDriver(name),
                        sim)