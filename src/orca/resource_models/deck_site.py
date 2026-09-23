from typing import List, Optional

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable, IPlateMover
from orca.resource_models.position_occupancy import PositionOccupancy


class DeckSite(ILabwarePlaceable):
    """Position marker for one site on a liquid handler deck.

    No driver of its own: a site under a device Location (parent_id +
    site_index from DeckLayoutConfig) is a place, and the ledger says what is
    at it.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._occupancy = PositionOccupancy(name)

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._occupancy.labware

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        return self._occupancy.loaded_labware

    @property
    def supports_deadlock_resolution(self) -> bool:
        return False

    def initialize_labware(self, labware: LabwareInstance) -> None:
        self._occupancy.arrive(labware)

    async def prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        pass

    async def prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        pass

    async def notify_picked(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        self._occupancy.leave(labware)

    async def notify_placed(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        self._occupancy.arrive(labware)

    async def dispose_labware(self, labware: LabwareInstance) -> None:
        self._occupancy.leave(labware)
