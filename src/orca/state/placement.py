"""Where a labware is, and whether an arm can reach it there.

One ledger, keyed by labware. `ILabwareLocationService` and the per-holder
`Location.labware` slot were the same fact kept twice, and two stores that can
disagree is what let a plate be at two places at once. Holders keep their driver
side effects and stop owning occupancy.

`PositionOccupancy` and the location service are the only two things that reach
it, which `tests/test_one_owner_per_physical_fact_guard.py` enforces.
"""

from dataclasses import dataclass
from enum import Enum

from orca.state.identity import LabwareRef, PositionRef


class Reach(Enum):
    """Whether an arm can take the labware from where it is.

    Not a state: a plate can be INSIDE while CLAIMED, PRESENT or RETIRED. It is
    the second half of occupancy, and leaving it out is why the staging bridge,
    the gripper pad and the plate pad each grew a field of their own.
    """

    AT_HAND = "at_hand"
    """A pad, a deck site, the jaws, a plate staged at the approach point."""

    INSIDE = "inside"
    """It occupies the position but is clamped in the device."""


class PlacementState(str, Enum):
    """What is settled about this labware's position.

    CLAIMED is "this position is taken for this labware, nothing else may have
    it, and nothing durable has been written yet" -- exactly what a
    slot-written-but-ledger-not-yet meant.

    "In transit" is deliberately absent. A carried plate is PRESENT at the
    mover's gripper position. Making it a state would make one state mean both
    "being carried" and "reserved in-lock", which are different conditions with
    different recoveries.
    """

    EXPECTED = "EXPECTED"
    CLAIMED = "CLAIMED"
    PRESENT = "PRESENT"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class Placement:
    """Where a labware is and how settled that is."""

    position: PositionRef
    reach: Reach
    state: PlacementState

    @property
    def is_occupying(self) -> bool:
        """Holds the position against anything else taking it."""
        return self.state in (PlacementState.CLAIMED, PlacementState.PRESENT)


class PlacementConflict(RuntimeError):
    """Something else already holds a position that admits one labware."""


class NotClaimed(RuntimeError):
    """A transition was asked for from a state that cannot make it."""


class PlacementLedger:
    """The one answer to where a labware is.

    Keyed by labware, so a position holding many labware falls out for free: a
    stacker or hotel holds a stack of plates at one position, which is why
    `loaded_labware` is a list today. Positions that admit one occupant assert a
    cardinality of one rather than modelling it.
    """

    def __init__(self) -> None:
        self._by_labware: dict[str, Placement] = {}
        self._name_by_id: dict[str, str] = {}
        # Kept alongside rather than derived: the mover asks what is at a
        # position on every hop, and walking every labware to answer is slow.
        # Insertion-ordered, because a stacker hands back its occupants in the
        # order they arrived and a set would shuffle them.
        self._at_position: dict[str, dict[str, None]] = {}

    # -- Transitions ---------------------------------------------------------

    def expect(self, labware: LabwareRef, at: PositionRef) -> None:
        """A labware is going to arrive here. Holds nothing against anyone."""
        self._write(labware, Placement(at, Reach.AT_HAND, PlacementState.EXPECTED))

    def stop_expecting(self, labware: LabwareRef) -> None:
        self._forget(labware)

    def claim(
        self, labware: LabwareRef, at: PositionRef, *, single_occupant: bool = True,
    ) -> None:
        """Take this position for this labware before anything durable is written.

        ``single_occupant`` is the caller's statement about the position, not the
        ledger's: a stacker passes False and stacks.
        """
        if single_occupant:
            self._refuse_if_taken(labware, at)
        self._write(labware, Placement(at, Reach.AT_HAND, PlacementState.CLAIMED))

    def abandon_claim(self, labware: LabwareRef) -> None:
        self._forget(labware)

    def arrived(
        self, labware: LabwareRef, at: PositionRef, *,
        reach: Reach = Reach.AT_HAND, single_occupant: bool = True,
    ) -> None:
        """It is physically there. The claim, if any, becomes the placement."""
        if single_occupant:
            self._refuse_if_taken(labware, at)
        self._write(labware, Placement(at, reach, PlacementState.PRESENT))

    def reached(self, labware: LabwareRef, reach: Reach) -> None:
        """The same labware at the same position, now clamped or released.

        Staging a plate at the approach point and loading it into the device is
        this call, not a second record. That pair was the staging bridge's whole
        reason to keep a store of its own.
        """
        held = self._by_labware.get(labware.id)
        if held is None or held.state is not PlacementState.PRESENT:
            raise NotClaimed(
                f"{labware.name!r} is not present anywhere, so its reach cannot change"
            )
        self._write(labware, Placement(held.position, reach, held.state))

    def vacate(self, labware: LabwareRef) -> None:
        """It is no longer here, and where it went is someone else's to say.

        Distinct from `retire`, which is the end of the labware's life. A pick
        vacates; spending the terminal state on it would leave the labware
        already retired when its thread really does end, and the write that
        clears it from the durable store would never fire.
        """
        self._forget(labware)

    def retire(self, labware: LabwareRef) -> None:
        """It has left the system. The record stays; the position is released."""
        held = self._by_labware.get(labware.id)
        if held is None:
            return
        self._release_position(labware.id, held.position.id)
        self._by_labware[labware.id] = Placement(
            held.position, held.reach, PlacementState.RETIRED,
        )

    # -- Reads ---------------------------------------------------------------

    def placement_of(self, labware: LabwareRef) -> Placement | None:
        """Where this labware is, or None when nothing has ever said."""
        return self._by_labware.get(labware.id)

    def occupants_of(self, position: PositionRef) -> list[LabwareRef]:
        """Every labware holding this position, in no particular order."""
        return [
            LabwareRef(id=lid, name=self._name_by_id[lid])
            for lid in self._at_position.get(position.id, {})
        ]

    def occupant_of(self, position: PositionRef) -> LabwareRef | None:
        """The single occupant, for a position that admits one.

        Raises rather than picking one when the position holds several: a caller
        asking this question about a stacker has the wrong question.
        """
        occupants = self.occupants_of(position)
        if not occupants:
            return None
        if len(occupants) > 1:
            raise PlacementConflict(
                f"position {position.id!r} holds {len(occupants)} labware; "
                f"ask for all of them"
            )
        return occupants[0]

    def accessible_at(self, position: PositionRef) -> list[LabwareRef]:
        """The occupants an arm could take from here right now."""
        return [
            ref for ref in self.occupants_of(position)
            if self._by_labware[ref.id].reach is Reach.AT_HAND
        ]

    # -- Internals -----------------------------------------------------------

    def _refuse_if_taken(self, labware: LabwareRef, at: PositionRef) -> None:
        holders = [h for h in self._at_position.get(at.id, {}) if h != labware.id]
        if holders:
            other = holders[0]
            raise PlacementConflict(
                f"position {at.id!r} already holds {self._name_by_id[other]!r}"
            )

    def _write(self, labware: LabwareRef, placement: Placement) -> None:
        previous = self._by_labware.get(labware.id)
        if previous is not None and previous.position.id != placement.position.id:
            self._release_position(labware.id, previous.position.id)
        self._by_labware[labware.id] = placement
        self._name_by_id[labware.id] = labware.name
        if placement.is_occupying:
            self._at_position.setdefault(placement.position.id, {})[labware.id] = None
        else:
            self._release_position(labware.id, placement.position.id)

    def _forget(self, labware: LabwareRef) -> None:
        held = self._by_labware.pop(labware.id, None)
        self._name_by_id.pop(labware.id, None)
        if held is not None:
            self._release_position(labware.id, held.position.id)

    def _release_position(self, labware_id: str, position_id: str) -> None:
        holders = self._at_position.get(position_id)
        if holders is None:
            return
        holders.pop(labware_id, None)
        if not holders:
            del self._at_position[position_id]
