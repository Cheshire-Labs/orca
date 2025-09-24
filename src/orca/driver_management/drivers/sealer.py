import logging
import asyncio

from pylabrobot.sealing.backend import SealerBackend as PLRSealerBackend
from abc import ABC, abstractmethod

orca_logger = logging.getLogger("orca")


class ISealerBackend(ABC):
    @abstractmethod
    async def setup(self) -> None:
        """Setup the sealer backend."""
        pass

    @abstractmethod
    async def open(self) -> None:
        """Open the sealer."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the sealer."""
        pass

    @abstractmethod
    async def seal(self, temperature: int, duration: float) -> None:
        """Seal at specified temperature and duration."""
        pass

    @abstractmethod
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature."""
        pass

    @abstractmethod
    async def get_temperature(self) -> float:
        """Get the current temperature."""
        pass


class PLRSealerBackendWrapper(ISealerBackend):
    def __init__(self, backend: PLRSealerBackend):
        self._backend = backend

    async def setup(self) -> None:
        await self._backend.setup()

    async def open(self) -> None:
        await self._backend.open()

    async def close(self) -> None:
        await self._backend.close()

    async def seal(self, temperature: int, duration: float) -> None:
        await self._backend.seal(temperature=temperature, duration=duration)

    async def set_temperature(self, temperature: float) -> None:
        await self._backend.set_temperature(temperature)

    async def get_temperature(self) -> float:
        return await self._backend.get_temperature()


class SimulationSealerBackend(ISealerBackend):
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





    

    