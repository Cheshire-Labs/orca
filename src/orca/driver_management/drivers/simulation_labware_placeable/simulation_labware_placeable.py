import logging
from typing import Optional

from orca.driver_management.drivers.simulation_base.simulation_base import SimulationBaseDriver
from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver

orca_logger = logging.getLogger("orca")
class SimulationLabwarePlaceableDriver(SimulationBaseDriver, ILabwarePlaceableDriver):

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Driver: {self._name} preparing for pick...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} prepared for pick")

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Driver: {self._name} preparing for place...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} prepared for place")

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Driver: {self._name} notified picked...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} notified picked")

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Driver: {self._name} notified placed...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} notified placed")