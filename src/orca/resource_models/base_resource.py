from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import logging
from orca_driver_interface.driver_interfaces import IDriver
from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver
from orca.resource_models.labware import Labware

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
    def labware(self) -> Optional[Labware]:
        raise NotImplementedError
    
    def initialize_labware(self, labware: Labware) -> None:
        # TODO: Make async in future
        # TODO: this will need to be restricted to only initilaizing the labware, probably with a LabwareManager service
        raise NotImplementedError
    
    @abstractmethod
    async def prepare_for_pick(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def prepare_for_place(self, labware: Labware) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def notify_placed(self, labware: Labware) -> None:
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
        self._stage_labware: Optional[Labware] = None
        self._loaded_labware: List[Labware] = []
    
    @property
    def stage(self) -> Optional[Labware]:
        return self._stage_labware
    
    @property
    def loaded_labware(self) -> List[Labware]:
        return self._loaded_labware

    def initialize_labware(self, labware: Labware) -> None:
        if labware in self._loaded_labware:
            return
        else:
            self._loaded_labware.append(labware)

    def unload_labware_to_stage(self, labware: Labware) -> None:
        if labware not in self._loaded_labware:
            raise ValueError(f"{self} - Labware {labware} not found in loaded labwares")
        if self._stage_labware is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to unload {labware}")
        self._loaded_labware.remove(labware)
        self._stage_labware = labware

    def load_labware_from_stage(self, labware: Labware) -> None:
        if self._stage_labware != labware:
            raise ValueError(f"{self} - Stage labware {self._stage_labware} does not match labware {labware} to load")
        self._stage_labware = None
        self._loaded_labware.append(labware)

    def set_stage(self, labware: Labware | None) -> None:
        self._stage_labware = labware
 
class LabwareLoadableEquipment(Equipment, ILabwarePlaceable):
    def __init__(self, name: str, driver: ILabwarePlaceableDriver) -> None:
        super().__init__(name, driver)
        self._driver: ILabwarePlaceableDriver = driver
        self._labware_reg = EquipmentLabwareRegistry()

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware_reg.stage
    
    @property
    def loaded_labware(self) -> List[Labware]:
        return self._labware_reg.loaded_labware
    
    def initialize_labware(self, labware: Labware) -> None:
        if labware in self._labware_reg.loaded_labware:
            return
        else:
            self._labware_reg.set_stage(labware)

    async def prepare_for_place(self, labware: Labware) -> None:
        if self._labware_reg.stage is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._labware_reg.stage}.  Unable to place {labware}")
        orca_logger.info(f"{self} - preparing for place of {labware}")
        await self._driver.prepare_for_place(labware.name, labware.labware_type)

    async def prepare_for_pick(self, labware: Labware) -> None:
        if self._labware_reg.stage == labware:
            return
        else:
            orca_logger.info(f"{self} - preparing for pick of {labware}")
            await self._driver.prepare_for_pick(labware.name, labware.labware_type)
            self._labware_reg.unload_labware_to_stage(labware)

    async def notify_picked(self, labware: Labware) -> None:
        if self._labware_reg.stage != labware:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as picked does not match the previously staged labware. "
                              "The wrong labware may have been picked.")
        orca_logger.info(f"{self} - labware {labware} picked from stage")
        self._labware_reg.set_stage(None)
        await self._driver.notify_picked(labware.name, labware.labware_type)
    
    async def notify_placed(self, labware: Labware) -> None:
        if self._labware_reg.stage is not None:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as placed was placed with labware {self._labware_reg.stage} already on the stage.  "
                             "A crash may have occurred.")
        self._labware_reg.set_stage(labware)
        orca_logger.info(f"{self} - labware {labware} received on stage")
        await self._driver.notify_placed(labware.name, labware.labware_type)
        self._labware_reg.load_labware_from_stage(labware)

    def __str__(self) -> str:
        return f"Equipment: {self._name}"

    