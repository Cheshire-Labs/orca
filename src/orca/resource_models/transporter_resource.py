import asyncio
import logging
from orca.resource_models.base_resource import Equipment
from orca_driver_interface.transporter_interfaces import ITransporterDriver
from orca.resource_models.location import Location
from typing import List, Optional
from orca.resource_models.labware import Labware

orca_logger = logging.getLogger("orca")

class TransporterEquipment(Equipment):
    def __init__(self, name: str, driver: ITransporterDriver) -> None:
        super().__init__(name, driver)
        self._driver: ITransporterDriver = driver
        self._labware: Optional[Labware] = None
        self._lock = asyncio.Lock()

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware
    
    async def pick(self, location: Location) -> None:
        async with self._lock:
            if self._labware is not None:
                raise ValueError(f"{self} already contains labware: {self._labware}")
            if location.labware is None:
                raise ValueError(f"{location} does not contain labware")
            orca_logger.info(f"{self._name} pick {location.labware} from {location}: picking...")
            await self._driver.pick(location.teachpoint_name, location.labware.labware_type)
            orca_logger.info(f"{self._name} pick {location.labware} from {location}: picked")
            self._labware = location.labware

    async def place(self, location: Location) -> None:
        async with self._lock:
            if self._labware is None:
                raise ValueError(f"{self} does not contain labware")
            if location.labware is not None:
                raise ValueError(f"{location} already contains labware")
            orca_logger.info(f"{self._name} place {self._labware} to {location}: placing...")
            await self._driver.place(location.teachpoint_name, self._labware.labware_type)
            orca_logger.info(f"{self._name} place {self._labware} to {location}: placed")
            
            self._labware = None

    def get_taught_positions(self) -> List[str]:
        return self._driver.get_taught_positions()
    
    def __str__(self) -> str:
        return f"Transporter: {self._name}"
    