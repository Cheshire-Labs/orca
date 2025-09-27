import logging
import asyncio

from orca.driver_management.drivers.driver_interfaces import ISealerDriver

orca_logger = logging.getLogger("orca")


# class SimulationSealerDriver:
#     def __init__(self, sim_time: float = 0.1):
#         self._simulated_temperature = 25.0  # Default simulated temperature
#         self._sim_time = sim_time

#     async def initialize(self) -> None:
#         """Simulate the setup of the sealer."""
#         orca_logger.info("Simulating setup of sealer")
#         await asyncio.sleep(self._sim_time)

#     async def open(self) -> None:
#         """Simulate opening the sealer."""
#         orca_logger.info("Simulating opening the sealer")
#         await asyncio.sleep(self._sim_time)

#     async def close(self) -> None:
#         """Simulate closing the sealer."""
#         orca_logger.info("Simulating closing the sealer")
#         await asyncio.sleep(self._sim_time)

#     async def seal(self, temperature: int, duration: float) -> None:
#         """Simulate sealing by just waiting for the duration."""
#         orca_logger.info(f"Simulating sealing at {temperature}°C for {duration} seconds")
#         await asyncio.sleep(self._sim_time)

#     async def set_temperature(self, temperature: float) -> None:
#         """Set the simulated temperature."""
#         orca_logger.info(f"Setting simulated temperature to {temperature}°C")
#         self._simulated_temperature = temperature   

#     async def get_temperature(self) -> float:
#         """Get the current simulated temperature."""
#         orca_logger.info(f"Getting simulated temperature: {self._simulated_temperature}°C")
#         return self._simulated_temperature





    

    