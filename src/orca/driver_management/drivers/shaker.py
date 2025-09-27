import logging


orca_logger = logging.getLogger("orca")


# class SimulationShakerDriver(IShakerDriver):
#     def __init__(self, sim_time: float = 0.1):
#         self._sim_time = sim_time

#     async def initialize(self) -> None:
#         """Simulate the setup of the shaker."""
#         orca_logger.info("Simulating setup of shaker")
#         await asyncio.sleep(self._sim_time)

#     async def stop(self) -> None:
#         """Simulate stopping the shaker."""
#         orca_logger.info("Simulating stopping the shaker")
#         await asyncio.sleep(self._sim_time)

#     @property
#     def supports_locking(self) -> bool:
#         """Check if the shaker supports locking the plate"""
#         return True

#     async def unlock_plate(self) -> None:
#         """Simulate unlocking the plate."""
#         orca_logger.info("Simulating unlocking the plate")
#         await asyncio.sleep(self._sim_time)

#     async def lock_plate(self):
#         """Simulate locking the plate."""
#         orca_logger.info("Simulating locking the plate")
#         await asyncio.sleep(self._sim_time)

#     async def shake(self, speed: float, duration: float) -> None:
#         """Simulate shaking by just waiting for the duration."""
#         orca_logger.info(f"Simulating shaking at {speed} RPM for {duration} seconds")
#         await asyncio.sleep(self._sim_time + duration)

#     async def stop_shaking(self) -> None:
#         """Simulate stopping shaking."""
#         orca_logger.info("Simulating stopping shaking")
#         await asyncio.sleep(self._sim_time)

