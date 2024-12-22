from typing import Any, Dict, Optional
from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver
from orca.driver_management.drivers.null_plate_pad.null_plate_pad import NullPlatePadDriver
from orca.resource_models.base_resource import ILabwarePlaceable, LabwareLoadableEquipment
from orca.resource_models.labware import Labware


class PlatePad(LabwareLoadableEquipment, ILabwarePlaceable):

    def __init__(self, name: str, driver: ILabwarePlaceableDriver = NullPlatePadDriver("Basic Plate Pad")) -> None:
        super().__init__(name, driver)
        self._is_initialized = False
        self._labware: Optional[Labware] = None

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware
    
    def initialize_labware(self, labware: Labware) -> None:
        self._labware = labware

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        return super().set_init_options(init_options)

    async def initialize(self) -> None:
        await self._driver.initialize()
        self._is_initialized = True
    
    async def notify_picked(self, labware: Labware) -> None:
        await self._driver.notify_picked(labware.name, labware.labware_type)
        self._labware = None
    
    async def notify_placed(self, labware: Labware) -> None:
        await self._driver.notify_placed(labware.name, labware.labware_type)
        self._labware = labware
        
    async def prepare_for_pick(self, labware: Labware) -> None:
        if self._labware != labware:
            raise ValueError(f"Labware {labware} not found on plate pad {self}")
        await self._driver.prepare_for_pick(labware.name, labware.labware_type)
    
    async def prepare_for_place(self, labware: Labware) -> None:
        if self._labware != None:
            raise ValueError(f"Trying to place Labware {labware}, but Labware {self._labware} already on plate pad {self}")
        await self._driver.prepare_for_place(labware.name, labware.labware_type)
