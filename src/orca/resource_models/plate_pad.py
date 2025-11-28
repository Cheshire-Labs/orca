import asyncio
from typing import Any, Optional
from cheshire_drivers import NullPlatePadDriver
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.resources import IInitializable, IResource
from orca.resource_models.labware import LabwareInstance


class PlatePad(IResource, IInitializable, ILabwarePlaceable):

    def __init__(self, name: str, driver: Any | None = None, supports_deadlock_resolution: bool = True) -> None:
        self._name = name
        self._supports_deadlock_resolution = supports_deadlock_resolution
        if driver is None:
            driver = NullPlatePadDriver(name)
        self._sim_manager = SimulationManager(driver,
                                              NullPlatePadDriver("Basic Plate Pad"),
                                              False)
        self._lock = asyncio.Lock()
        self._is_initialized = False
        self._labware: Optional[LabwareInstance] = None
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_initialized(self) -> bool:
        return self._sim_manager.driver.is_initialized

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._labware

    @property
    def supports_deadlock_resolution(self) -> bool:
        return self._supports_deadlock_resolution

    def initialize_labware(self, labware: LabwareInstance) -> None:
        self._labware = labware

    async def initialize(self) -> None:
        await self._sim_manager.driver.initialize()
        self._is_initialized = True
    
    async def notify_picked(self, labware: LabwareInstance) -> None:
        await self._sim_manager.driver.notify_picked(labware.name, labware.labware_type)
        self._labware = None

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        await self._sim_manager.driver.notify_picked(labware.name, labware.labware_type)

    
    async def notify_placed(self, labware: LabwareInstance) -> None:
        await self._sim_manager.driver.notify_placed(labware.name, labware.labware_type)
        self._labware = labware
        
    async def prepare_for_pick(self, labware: LabwareInstance) -> None:
        if self._labware != labware:
            raise ValueError(f"Labware {labware} not found on plate pad {self}")
        await self._sim_manager.driver.prepare_for_pick(labware.name, labware.labware_type)
    
    async def prepare_for_place(self, labware: LabwareInstance) -> None:
        if self._labware != None:
            raise ValueError(f"Trying to place Labware {labware}, but Labware {self._labware} already on plate pad {self}")
        await self._sim_manager.driver.prepare_for_place(labware.name, labware.labware_type)
