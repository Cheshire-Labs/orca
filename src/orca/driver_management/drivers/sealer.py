import logging
import asyncio
from typing import Dict

from orca.driver_management.driver_interfaces import ISealer
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager
from pylabrobot.sealing.backend import SealerBackend

orca_logger = logging.getLogger("orca")


class SimulationSealerBackend:
    def __init__(self, port: str, timeout: int = 20, sim_time: float = 0.1):
        self._simulated_temperature = 25.0  # Default simulated temperature
        self._sim_time = sim_time

    async def setup(self) -> None:
        """Simulate the setup of the sealer."""
        orca_logger.info("Simulating setup of sealer")
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
        await asyncio.sleep(self._sim_time)

    async def set_temperature(self, temperature: float) -> None:
        """Set the simulated temperature."""
        orca_logger.info(f"Setting simulated temperature to {temperature}°C")
        self._simulated_temperature = temperature   

    async def get_temperature(self) -> float:
        """Get the current simulated temperature."""
        orca_logger.info(f"Getting simulated temperature: {self._simulated_temperature}°C")
        return self._simulated_temperature


class Sealer(Device, ISealer):
    def __init__(self, name: str, backend: SealerBackend, sim: bool = False) -> None:
        self._name = name
        self._sim_manager = SimulationManager(
            backend,
            SimulationSealerBackend("sim", sim_time=0.1),
            sim
            )
        super().__init__(name, self._sim_manager)

    @property
    def driver(self) -> SealerBackend | SimulationSealerBackend:
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
        await self.driver.setup(**self._init_options)  # type: ignore
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

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        await self.driver.open()

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        await self.driver.open()

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        await self.driver.close()

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        await self.driver.close()

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        await self.driver.seal(temperature=temperature, duration=duration)



    

    