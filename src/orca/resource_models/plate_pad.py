from typing import List, Optional

from cheshire_drivers.null_plate_pad import NullPlatePadDriver

from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable, IPlateMover
from orca.resource_models.labware_directory import LabwarePlacements
from orca.resource_models.position_occupancy import PositionOccupancy
from orca.resource_models.resources import IInitializable, IResource


class PlatePad(IResource, IInitializable, ILabwarePlaceable):

    def __init__(
        self,
        name: str,
        driver: NullPlatePadDriver | None = None,
        supports_deadlock_resolution: bool = True,
    ) -> None:
        self._name = name
        self._supports_deadlock_resolution = supports_deadlock_resolution
        if driver is None:
            driver = NullPlatePadDriver(name)
        self._driver = driver
        self._is_initialized = False
        self._occupancy = PositionOccupancy(name)

    @property
    def name(self) -> str:
        return self._name

    def bind_placements(self, placements: LabwarePlacements) -> None:
        self._occupancy.bind_placements(placements)

    @property
    def is_initialized(self) -> bool:
        return self._driver.is_initialized

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._occupancy.labware

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        return self._occupancy.loaded_labware

    @property
    def supports_deadlock_resolution(self) -> bool:
        return self._supports_deadlock_resolution

    def initialize_labware(self, labware: LabwareInstance) -> None:
        self._occupancy.arrive(labware)

    async def initialize(self) -> None:
        await self._driver.initialize()
        self._is_initialized = True

    async def notify_picked(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        if labware is None:
            return
        await self._driver.notify_picked(labware.name, labware.labware_type)
        self._occupancy.leave(labware)

    async def _do_notify_picked(self, labware: LabwareInstance | None) -> None:
        if labware is not None:
            await self._driver.notify_picked(labware.name, labware.labware_type)

    async def dispose_labware(self, labware: LabwareInstance) -> None:
        self._occupancy.leave(labware)

    async def notify_placed(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        if labware is None:
            return
        await self._driver.notify_placed(labware.name, labware.labware_type)
        self._occupancy.arrive(labware)

    async def prepare_for_pick(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        if labware is None:
            return
        if not self._occupancy.holds(labware):
            raise ValueError(f"Labware {labware} not found on plate pad {self}")
        await self._driver.prepare_for_pick(labware.name, labware.labware_type)

    async def prepare_for_place(self, labware: LabwareInstance | None, mover: IPlateMover) -> None:
        occupant = self._occupancy.labware
        if occupant is not None and occupant is not labware:
            raise SlotOccupiedError(self._name, occupant.name, occupant.template_name)
        if labware is not None:
            await self._driver.prepare_for_place(labware.name, labware.labware_type)
