from abc import ABC, abstractmethod
import asyncio
import logging
from typing import List, Optional
from orca.resource_models.transporter_interface import ITransporter
from orca.resource_models.resources import ISimulationable
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location

orca_logger = logging.getLogger("orca")

class Transporter(ITransporter, ABC):
    def __init__(self,
                name: str,
                sim_manager: ISimulationable) -> None:
        self._name = name
        self._sim_manager = sim_manager
        self._lock = asyncio.Lock()
        self._labware: Optional[LabwareInstance] = None

    @property
    def name(self) -> str:
        """Returns the name of the transporter."""
        return self._name

    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """Returns whether the transporter is initialized or not."""
        raise NotImplementedError("This method should be implemented by subclasses.")
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initializes the transporter."""
        raise NotImplementedError("This method should be implemented by subclasses.")

    @property
    def lock(self) -> asyncio.Lock:
        """Returns the lock for the transporter."""
        return self._lock

    @property
    def in_use(self) -> bool:
        """Returns whether the transporter is running or not."""
        return self._lock.locked()

    @property
    def is_simulating(self) -> bool:
        """Returns whether the transporter is simulating or not."""
        return self._sim_manager.is_simulating
    
    def set_simulating(self, sim: bool) -> None:
        """Sets the simulation state of the transporter."""
        self._sim_manager.set_simulating(sim)

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._labware
    
    async def pick(self, location: Location) -> None:
        if self._labware is not None:
            raise ValueError(f"{self} already contains labware: {self._labware}")
        if location.labware is None:
            raise ValueError(f"{location} does not contain labware")
        orca_logger.info(f"{self._name} pick {location.labware} from {location}: picking...")
        await self._do_pick(location.teachpoint_name, location.labware.labware_type)
        orca_logger.info(f"{self._name} pick {location.labware} from {location}: picked")
        self._labware = location.labware

    async def place(self, location: Location) -> None:
        if self._labware is None:
            raise ValueError(f"{self} does not contain labware")
        if location.labware is not None:
            raise ValueError(f"{location} already contains labware")
        orca_logger.info(f"{self._name} place {self._labware} to {location}: placing...")
        await self._do_place(location.teachpoint_name, self._labware.labware_type)
        orca_logger.info(f"{self._name} place {self._labware} to {location}: placed")
        
        self._labware = None

    async def _do_pick(self, teachpoint_name: str, labware_type: str) -> None:
        raise NotImplementedError("This method should be implemented by subclasses.")
    
    async def _do_place(self, teachpoint_name: str, labware_type: str) -> None:
        raise NotImplementedError("This method should be implemented by subclasses.")

    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError("This method should be implemented by subclasses.")
    
    
    def __str__(self) -> str:
        return f"Transporter: {self._name}"