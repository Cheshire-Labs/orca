"""One store for where a labware is, and what its four states mean.

Nothing consumes this yet; the holders still own occupancy and PR-2 migrates
them. These pin the behaviour the migration will depend on, so a change to the
ledger cannot quietly change what a holder is being replaced with.

The two decisions worth reading here:

- **A placement is a pair.** Where it is, and whether an arm can take it from
  there. A plate clamped inside a reader occupies its site and cannot be picked;
  staged at the approach point it occupies the same site and can. Leaving that
  out is why the staging bridge kept a store of its own.
- **A position may hold many labware.** A stacker holds a column of plates at
  one position, which is why the holder's field is a list today. The ledger is
  keyed by labware, so many-to-one falls out and a single-occupant position
  asserts a cardinality rather than modelling one.
"""

import pytest

from orca.state.identity import LabwareRef, PositionRef
from orca.state.placement import (
    NotClaimed,
    PlacementConflict,
    PlacementLedger,
    PlacementState,
    Reach,
)


PLATE = LabwareRef(id="id-plate-1", name="sample_plate-1")
OTHER = LabwareRef(id="id-plate-2", name="sample_plate-2")
PAD = PositionRef(id="pad1")
READER = PositionRef(id="reader1")
STACK = PositionRef(id="stacker1")


class TestTheLifecycle:
    def test_a_labware_nothing_has_said_about_has_no_placement(self) -> None:
        assert PlacementLedger().placement_of(PLATE) is None

    def test_expecting_does_not_hold_the_position(self) -> None:
        """EXPECTED is an intention. Holding a position on the strength of one
        would block a slot for a labware that may never come."""
        ledger = PlacementLedger()
        ledger.expect(PLATE, PAD)

        assert ledger.placement_of(PLATE).state is PlacementState.EXPECTED
        assert ledger.occupants_of(PAD) == []
        ledger.claim(OTHER, PAD)

    def test_claiming_holds_it_before_anything_durable_is_written(self) -> None:
        ledger = PlacementLedger()
        ledger.claim(PLATE, PAD)

        assert ledger.placement_of(PLATE).state is PlacementState.CLAIMED
        assert ledger.occupants_of(PAD) == [PLATE]
        with pytest.raises(PlacementConflict):
            ledger.claim(OTHER, PAD)

    def test_arriving_settles_the_claim(self) -> None:
        ledger = PlacementLedger()
        ledger.claim(PLATE, PAD)
        ledger.arrived(PLATE, PAD)

        assert ledger.placement_of(PLATE).state is PlacementState.PRESENT
        assert ledger.occupants_of(PAD) == [PLATE]

    def test_abandoning_a_claim_frees_the_position(self) -> None:
        ledger = PlacementLedger()
        ledger.claim(PLATE, PAD)
        ledger.abandon_claim(PLATE)

        assert ledger.occupants_of(PAD) == []
        ledger.claim(OTHER, PAD)

    def test_retiring_keeps_the_last_known_position_readable(self) -> None:
        """It left, and where it was is still worth being able to ask."""
        ledger = PlacementLedger()
        ledger.arrived(PLATE, PAD)
        ledger.retire(PLATE)

        placement = ledger.placement_of(PLATE)
        assert placement.state is PlacementState.RETIRED
        assert placement.position == PAD
        assert ledger.occupants_of(PAD) == []


class TestBeingCarriedIsNotAState:
    def test_a_plate_in_the_jaws_is_present_at_the_jaws(self) -> None:
        """Naming it a state would make one state mean both "being carried" and
        "reserved in-lock", which recover differently."""
        ledger = PlacementLedger()
        jaws = PositionRef(id="robot1-gripper")
        ledger.arrived(PLATE, PAD)
        ledger.arrived(PLATE, jaws)

        assert ledger.placement_of(PLATE).state is PlacementState.PRESENT
        assert ledger.placement_of(PLATE).position == jaws
        assert ledger.occupants_of(PAD) == [], "the source must be vacated"


class TestReachIsTheOtherHalfOfOccupancy:
    def test_a_clamped_plate_occupies_its_site_and_cannot_be_taken(self) -> None:
        ledger = PlacementLedger()
        ledger.arrived(PLATE, READER, reach=Reach.INSIDE)

        assert ledger.occupants_of(READER) == [PLATE]
        assert ledger.accessible_at(READER) == []

    def test_staging_and_loading_are_one_record(self) -> None:
        """The pair the staging bridge kept a second store for."""
        ledger = PlacementLedger()
        ledger.arrived(PLATE, READER)
        assert ledger.accessible_at(READER) == [PLATE]

        ledger.reached(PLATE, Reach.INSIDE)
        assert ledger.occupants_of(READER) == [PLATE]
        assert ledger.accessible_at(READER) == []

        ledger.reached(PLATE, Reach.AT_HAND)
        assert ledger.accessible_at(READER) == [PLATE]

    def test_reach_cannot_change_on_something_that_is_not_there(self) -> None:
        ledger = PlacementLedger()
        ledger.claim(PLATE, READER)
        with pytest.raises(NotClaimed):
            ledger.reached(PLATE, Reach.INSIDE)


class TestAPositionMayHoldMany:
    def test_a_stacker_holds_a_column(self) -> None:
        ledger = PlacementLedger()
        ledger.arrived(PLATE, STACK, single_occupant=False)
        ledger.arrived(OTHER, STACK, single_occupant=False)

        assert {ref.id for ref in ledger.occupants_of(STACK)} == {PLATE.id, OTHER.id}

    def test_asking_a_stacker_for_its_one_occupant_refuses(self) -> None:
        """A caller asking that question about a stacker has the wrong one."""
        ledger = PlacementLedger()
        ledger.arrived(PLATE, STACK, single_occupant=False)
        ledger.arrived(OTHER, STACK, single_occupant=False)

        with pytest.raises(PlacementConflict):
            ledger.occupant_of(STACK)

    def test_an_empty_position_has_no_occupant(self) -> None:
        assert PlacementLedger().occupant_of(PAD) is None


class TestMovingVacatesTheSource:
    def test_one_write_moves_it_rather_than_placing_it_twice(self) -> None:
        """Two stores is how a plate came to be at two places at once. One
        write cannot leave it at both."""
        ledger = PlacementLedger()
        ledger.arrived(PLATE, PAD)
        ledger.arrived(PLATE, READER)

        assert ledger.occupants_of(PAD) == []
        assert ledger.occupants_of(READER) == [PLATE]

    def test_re_arriving_where_it_already_is_is_not_a_conflict(self) -> None:
        ledger = PlacementLedger()
        ledger.arrived(PLATE, PAD)
        ledger.arrived(PLATE, PAD)

        assert ledger.occupants_of(PAD) == [PLATE]


class TestAPickIsNotTheEndOfALife:
    """Vacating and retiring are different, and spending the terminal state on
    a pick left a completed thread's plate persisted on its end slot: the
    retire that clears it fires once, and it had already been spent."""

    def test_vacating_leaves_no_placement_to_read(self) -> None:
        ledger = PlacementLedger()
        plate = LabwareRef(id="id-1", name="plate_1")
        ledger.arrived(plate, PositionRef(id="pad_1"))

        ledger.vacate(plate)

        assert ledger.placement_of(plate) is None
        assert ledger.occupant_of(PositionRef(id="pad_1")) is None

    def test_retiring_keeps_the_record_and_releases_the_position(self) -> None:
        ledger = PlacementLedger()
        plate = LabwareRef(id="id-1", name="plate_1")
        ledger.arrived(plate, PositionRef(id="pad_1"))

        ledger.retire(plate)

        held = ledger.placement_of(plate)
        assert held is not None and held.state is PlacementState.RETIRED
        assert ledger.occupant_of(PositionRef(id="pad_1")) is None

    def test_a_position_holding_many_answers_in_arrival_order(self) -> None:
        """A stacker hands its occupants back in the order they arrived; a set
        shuffled them, so `labware` picked an arbitrary one."""
        ledger = PlacementLedger()
        at = PositionRef(id="stacker_out")
        for index in range(6):
            ledger.arrived(
                LabwareRef(id=f"id-{index}", name=f"plate_{index}"),
                at, single_occupant=False,
            )

        assert [ref.name for ref in ledger.occupants_of(at)] == [
            f"plate_{index}" for index in range(6)
        ]
