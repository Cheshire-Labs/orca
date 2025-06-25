from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import logging
from orca_driver_interface.driver_interfaces import IDriver
from orca.resource_models.device_error import DeviceBusyError
from orca.resource_models.labware import LabwareInstance

orca_logger = logging.getLogger("orca")

class ResourceUnavailableError(Exception):
    def __init__(self, message: str = "Resource is unavailable.") -> None:
        super().__init__(message)
    
    
class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    

class IInitializableResource(IResource, ABC):
    
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

class IEquipment(IInitializableResource, ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        raise NotImplementedError
    

class ILabwarePlaceable(ABC):
    @property
    def name(self) -> str:
        raise NotImplementedError
    
    @property
    def labware(self) -> Optional[LabwareInstance]:
        raise NotImplementedError
    
    def initialize_labware(self, labware: LabwareInstance) -> None:
        # TODO: Make async in future
        # TODO: this will need to be restricted to only initilaizing the labware, probably with a LabwareManager service
        raise NotImplementedError
    
    @abstractmethod
    async def prepare_for_pick(self, labware: LabwareInstance) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def prepare_for_place(self, labware: LabwareInstance) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware: LabwareInstance) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def notify_placed(self, labware: LabwareInstance) -> None:
        raise NotImplementedError


class Equipment(IEquipment):

    def __init__(self, name: str, driver: IDriver) -> None:
        self._name = name
        self._driver = driver

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_initialized(self) -> bool:
        return self._driver.is_initialized
    
    @property
    def is_running(self) -> bool:
        return self._driver.is_running
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._driver.set_init_options(init_options)
    
    async def initialize(self) -> None:
        orca_logger.info(f"Initializing...")
        orca_logger.info(f"Name: {self._name}")
        await self._driver.initialize()
        orca_logger.info(f"Initialized")

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        if command is None:
            raise ValueError(f"{self} - No command to execute")
        orca_logger.info(f"{self} - execute - {command}")
        orca_logger.info(f"{self} - {command} executing...")
        await self._driver.execute(command, options)
        orca_logger.info(f"{self} - {command} executed")

    @property
    def is_connected(self) -> bool:
        return self._driver.is_connected

    async def connect(self) -> None:
        orca_logger.info(f"{self} - Connecting...")
        await self._driver.connect()
        orca_logger.info(f"{self} - Connected")

    async def disconnect(self) -> None:
        orca_logger.info(f"{self} - Disconnecting...")
        await self._driver.disconnect()
        orca_logger.info(f"{self} - Disconnected")



class EquipmentLabwareRegistry:
    def __init__(self) -> None:
        self._stage_labware: Optional[LabwareInstance] = None
        self._loaded_labware: List[LabwareInstance] = []
    
    @property
    def stage(self) -> Optional[LabwareInstance]:
        return self._stage_labware
    
    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        return self._loaded_labware

    def initialize_labware(self, labware: LabwareInstance) -> None:
        if labware in self._loaded_labware:
            return
        else:
            self._loaded_labware.append(labware)

    def unload_labware_to_stage(self, labware: LabwareInstance) -> None:
        if labware not in self._loaded_labware:
            raise ValueError(f"{self} - Labware {labware} not found in loaded labwares")
        if self._stage_labware is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to unload {labware}")
        self._loaded_labware.remove(labware)
        self._stage_labware = labware

    def load_labware_from_stage(self, labware: LabwareInstance) -> None:
        if self._stage_labware != labware:
            raise ValueError(f"{self} - Stage labware {self._stage_labware} does not match labware {labware} to load")
        self._stage_labware = None
        self._loaded_labware.append(labware)

    def set_stage(self, labware: LabwareInstance | None) -> None:
        if self._stage_labware is not None and labware is not None:
            raise DeviceBusyError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to set stage to {labware}")
        self._stage_labware = labware


class ISimulationable(ABC):
    @property
    @abstractmethod
    def is_simulating(self) -> bool:
        """Returns whether the resource is simulating or not."""
        raise NotImplementedError

    @abstractmethod
    def set_simulating(self, simulating: bool) -> None:
        """Sets the simulation state of the resource."""
        raise NotImplementedError
 

    