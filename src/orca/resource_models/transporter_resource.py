import asyncio
import logging
from orca.driver_management.drivers.simulation_robotic_arm.simulation_robotic_arm import SimulationRoboticArmDriver
from orca.resource_models.base_resource import Equipment, ISimulationable
from orca_driver_interface.transporter_interfaces import ITransporterDriver
from orca.resource_models.location import Location
from typing import List, Optional
from orca.resource_models.labware import LabwareInstance

orca_logger = logging.getLogger("orca")


class TransporterEquipment(Equipment, ISimulationable):
    """
    Represents a transporter equipment capable of picking and placing labware between locations.
    """
    def __init__(self, name: str, driver: ITransporterDriver, sim: bool = False) -> None:
        """Initialize the transporter equipment with a name and a driver.
        Args:
            name (str): The name of the transporter equipment.
    `1 VBGR            driver (ITransporterDriver): The driver that implements the transporter's functionality.
        """
        super().__init__(name, driver)
        self._live_driver: ITransporterDriver = driver
        self._sim_driver: ITransporterDriver = SimulationRoboticArmDriver(name, driver.name, driver.get_taught_positions())
        self._driver: ITransporterDriver = self._live_driver
        self._labware: Optional[LabwareInstance] = None
        self._lock = asyncio.Lock()
        self._is_simulating: bool = False
        self.set_simulating(sim)

    @property
    def is_simulating(self) -> bool:
        """Returns whether the transporter is simulating or not."""
        return self._is_simulating

    def set_simulating(self, simulating: bool) -> None:
        self._is_simulating = simulating
        if self._is_simulating:
            self._driver = self._sim_driver
        else:
            self._driver = self._live_driver

    @property
    def labware(self) -> Optional[LabwareInstance]:
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
    