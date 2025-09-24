import logging


orca_logger = logging.getLogger("orca")

class SimulationShakerBackend:
    def __init__(self):
        self._is_shaking = False

    async def lock_plate(self) -> None:
        orca_logger.info("Simulated locking plate")

    async def unlock_plate(self) -> None:
        orca_logger.info("Simulated unlocking plate")

    async def shake(self, speed: int) -> None:
        orca_logger.info(f"Simulating shaking at speed {speed}")

    async def stop_shaking(self) -> None:
        orca_logger.info("Simulating stopping shaking")

