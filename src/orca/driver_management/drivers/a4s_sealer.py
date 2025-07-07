import asyncio

import logging
from orca.driver_management.driver_interfaces import ISealer, ITempGettable, ITempSettable
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


class A4SSealer(Device, ISealer, ITempGettable, ITempSettable):
    def __init__(
        self,
        name: str,
        port: str,
        timeout: int = 20,
        sim: bool = False
    ):
        self._sim_manager = SimulationManager(
            live_driver=A4SBackend(port, timeout),
            sim_driver=SimulationA4SBackend(port, timeout),
            sim=sim
        )
        self._is_initialized = False
        super().__init__(name, 
                         self._sim_manager)
    @property  
    def driver(self) -> A4SBackend | SimulationA4SBackend:
         return self._sim_manager.driver

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        await self.driver.open()

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        await self.driver.open()

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        await self.driver.close()

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        await self.driver.close()

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    async def initialize(self) -> None:
        """Initialize the sealer."""
        await self.driver.setup()
        self._is_initialized = True
        orca_logger.info(f"{self.name} initialized successfully.")

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        await self.driver.seal(temperature, duration)

    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the sealer."""
        await self.driver.set_temperature(temperature)

    async def get_temperature(self) -> float:
        """Get the current temperature of the sealer."""
        return await self.driver.get_temperature()