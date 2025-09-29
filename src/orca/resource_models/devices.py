from abc import ABC
import asyncio
import logging
from orca.driver_management.drivers.driver_interfaces import BaseDriver
from orca.resource_models.resources import IDevice
from orca.resource_models.device_error import DeviceBusyError
from orca.resource_models.labware import LabwareInstance
from typing import Generic, List, Optional, TypeVar

from orca.resource_models.simulation_manager import SimulationManager

orca_logger = logging.getLogger("orca")

class StageLoadingEquipmentLabwareManager:
    def __init__(self) -> None:
        self._stage_labware: Optional[LabwareInstance] = None
        self._loaded_labware: List[LabwareInstance] = []

    @property
    def staged_labware(self) -> Optional[LabwareInstance]:
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

    def set_staged_labware(self, labware: LabwareInstance | None) -> None:
        if self._stage_labware is not None and labware is not None:
            raise DeviceBusyError(f"{self} - Stage already contains labware: {self._stage_labware}.  Unable to set stage to {labware}")
        self._stage_labware = labware


TDriver = TypeVar('TDriver', bound=BaseDriver)
class Device(IDevice, Generic[TDriver], ABC):
    """A class that represents a device that can operate on labware."""
    def __init__(self, 
                 name: str, 
                 driver: TDriver,
                 sim_driver: TDriver,
                 sim: bool = False,
                 labware_reg: StageLoadingEquipmentLabwareManager | None = None,
                 ) -> None:
        """Initialize the device with a name and a driver.
        Args:
            name (str): The name of the device.
            sim_manager (ISimulationable): The simulation manager for the device.
            labware_reg (StageLoadingEquipmentLabwareManager, optional): The labware manager for the device. Defaults to None.
        """
        self._name = name
        self._sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim
            )
        self._is_initialized = False
        self._labware_reg = StageLoadingEquipmentLabwareManager() if labware_reg is None else labware_reg
        self._lock = asyncio.Lock() 

    @property
    def name(self) -> str:
        """Returns the name of the device."""
        return self._name

    @property
    def driver(self) -> TDriver:
        """Returns the device's driver."""
        return self._sim_manager.driver

    @property
    def lock(self) -> asyncio.Lock:
        """Returns the lock for the device."""
        return self._lock
    
    @property
    def is_initialized(self) -> bool:
        return self.driver.is_initialized
    
    async def initialize(self) -> None:
        await self.driver.initialize()

    @property
    def in_use(self) -> bool:
        """Returns whether the device is running or not."""
        return self._lock.locked()

    @property
    def is_simulating(self) -> bool:
        """Returns whether the device is simulating or not."""
        return self._sim_manager.is_simulating

    def set_simulating(self, sim: bool) -> None:
        """Sets the simulation state of the device."""
        self._sim_manager.set_simulating(sim)

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._labware_reg.staged_labware

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        return self._labware_reg.loaded_labware

    def initialize_labware(self, labware: LabwareInstance) -> None:
        if labware in self._labware_reg.loaded_labware:
            return
        else:
            self._labware_reg.set_staged_labware(labware)

    async def prepare_for_place(self, labware: LabwareInstance) -> None:
        if self._labware_reg.staged_labware is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._labware_reg.staged_labware}.  Unable to place {labware}")
        orca_logger.info(f"{self} - preparing for place of {labware}")
        await self._do_prepare_for_place(labware)

    async def prepare_for_pick(self, labware: LabwareInstance) -> None:

        if self._labware_reg.staged_labware == labware:
            return
        else:
            orca_logger.info(f"{self} - preparing for pick of {labware}")
            await self._do_prepare_for_pick(labware)
            self._labware_reg.unload_labware_to_stage(labware)

    async def notify_picked(self, labware: LabwareInstance) -> None:
        if self._labware_reg.staged_labware != labware:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as picked does not match the previously staged labware. "
                            "The wrong labware may have been picked.")
        orca_logger.info(f"{self} - labware {labware} picked from stage")
        self._labware_reg.set_staged_labware(None)
        await self._do_notify_picked(labware)

    async def notify_placed(self, labware: LabwareInstance) -> None:
        if self._labware_reg.staged_labware is not None:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as placed was placed with labware {self._labware_reg.staged_labware} already on the stage.  "
                            "A crash may have occurred.")
        self._labware_reg.set_staged_labware(labware)
        orca_logger.info(f"{self} - labware {labware} received on stage")
        await self._do_notify_placed(labware)
        self._labware_reg.load_labware_from_stage(labware)

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        """Prepare the device for picking up the specified labware."""
        await self.driver.open()

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        """Prepare the device for picking up the specified labware."""
        await self.driver.open()

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        """Notify the device that the specified labware has been picked."""
        await self.driver.close()

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        """Notify the device that the specified labware has been placed."""
        await self.driver.close()

    def __str__(self) -> str:
        return f"Equipment: {self._name}"
    