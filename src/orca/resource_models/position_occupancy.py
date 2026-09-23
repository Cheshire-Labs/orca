"""What is at one position, answered from the one ledger.

Every holder used to keep this itself, in a `_labware` field or a staged/loaded
pair, and the refusal to double-book was written out four times. A holder now
composes this and keeps only its driver hooks.
"""

from typing import List, Optional

from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_directory import labware_directory
from orca.state.current import placement_ledger
from orca.state.identity import PositionRef
from orca.state.placement import PlacementConflict, Reach


class PositionOccupancy:
    """The occupancy half of a holder, bound to one position."""

    def __init__(self, position_id: str, *, single_occupant: bool = True) -> None:
        self._position = PositionRef(id=position_id)
        self._single = single_occupant

    @property
    def labware(self) -> Optional[LabwareInstance]:
        """The occupant, staged or loaded. A loaded plate still occupies its
        site: loading is a clamp on the same physical position, not a second."""
        occupants = self._occupants()
        return occupants[0] if occupants else None

    @property
    def accessible_labware(self) -> Optional[LabwareInstance]:
        """What an arm could take from here right now. A loaded plate is inside
        the device until something stages it back out."""
        directory = labware_directory()
        reachable = placement_ledger().accessible_at(self._position)
        return directory.resolve(reachable[0]) if reachable else None

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        return self._occupants()

    @property
    def inside_labware(self) -> List[LabwareInstance]:
        """The occupants clamped in the device rather than at its approach
        point. The staged/loaded pair a bridge used to keep is this reach."""
        ledger = placement_ledger()
        directory = labware_directory()
        inside = []
        for ref in ledger.occupants_of(self._position):
            held = ledger.placement_of(ref)
            if held is not None and held.reach is Reach.INSIDE:
                inside.append(directory.resolve(ref))
        return inside

    def arrive(self, labware: LabwareInstance, *, reach: Reach = Reach.AT_HAND) -> None:
        """Record the labware here, refusing a position another one holds."""
        labware_directory().remember(labware)
        try:
            placement_ledger().arrived(
                labware.ref, self._position,
                reach=reach, single_occupant=self._single,
            )
        except PlacementConflict:
            occupant = self.labware
            raise SlotOccupiedError(
                position_id=self._position.id,
                existing_labware_name=occupant.name if occupant else "",
                existing_template_name=occupant.template_name if occupant else "",
            ) from None

    def reached(self, labware: LabwareInstance, reach: Reach) -> None:
        """The same labware here, now clamped in or released to the approach
        point. Not a second record."""
        placement_ledger().reached(labware.ref, reach)

    def leave(self, labware: LabwareInstance) -> None:
        """It is gone from here. Silent when it was never recorded, because a
        pick that follows a failed place has nothing to undo.

        Vacating, never retiring: the end of a labware's life is the location
        service's to record, and spending the terminal state on a pick left the
        plate already retired when its thread ended, so nothing cleared it from
        the durable store and the next boot put it back on the slot.
        """
        ledger = placement_ledger()
        held = ledger.placement_of(labware.ref)
        if held is None or held.position.id != self._position.id:
            return
        ledger.vacate(labware.ref)

    def holds(self, labware: LabwareInstance) -> bool:
        held = placement_ledger().placement_of(labware.ref)
        return (
            held is not None
            and held.is_occupying
            and held.position.id == self._position.id
        )

    def _occupants(self) -> List[LabwareInstance]:
        directory = labware_directory()
        return [
            directory.resolve(ref)
            for ref in placement_ledger().occupants_of(self._position)
        ]
