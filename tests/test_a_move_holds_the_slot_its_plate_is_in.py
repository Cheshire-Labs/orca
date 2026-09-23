"""A move keeps the slot its plate has not left yet.

A liquid handler's own gripper carries a plate with ONE driver call, issued at
place time. Its pick actuates nothing. The record hands the plate to the jaws
the instant that pick returns, so for the whole of the move the slot reads free
while the plate is standing in it.

On the bench that window was 18 seconds. The next thread was granted the source
the moment the gripper "picked", and its arm was on its way to a slot the
gripper was still lifting out of.
"""

import asyncio
from typing import List, Optional
from unittest.mock import Mock

import pytest

from orca.events.execution_context import ThreadExecutionContext
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.system_map import ILocationRegistry
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter_base import TransporterBase
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
from orca.workflow_models.actions.move_action import MoveAction
from tests.test_helpers import (
    create_test_labware_instance,
    make_labware_placer,
)
from orca.resource_models.labware_location_service import PlacementState
from cheshire_drivers.teachpoints import Teachpoint

pytestmark = pytest.mark.asyncio

CARRIER = "thread-carrying"
NEXT_IN_LINE = "thread-next"


class _TwoSlotRegistry(ILocationRegistry):
    def __init__(self, locations: List[Location]) -> None:
        self._by_id = {loc.position_id: loc for loc in locations}

    @property
    def locations(self) -> List[Location]:
        return list(self._by_id.values())

    def get_location(self, name: str) -> Location:
        return self._by_id[name]

    def add_location(self, location: Location) -> None:
        raise NotImplementedError


class _CarriesInOneCall(TransporterBase):
    """A handler's own deck gripper: the pick actuates nothing and the whole
    carry happens in the place, which blocks until the test lets it finish."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.placing = asyncio.Event()
        self.may_finish = asyncio.Event()
        self.fail_with: Optional[Exception] = None

    @property
    def pick_moves_the_plate(self) -> bool:
        return False

    async def initialize(self) -> None:
        return

    @property
    def is_initialized(self) -> bool:
        return True

    async def get_teachpoints(self) -> list[Teachpoint]:
        return []

    async def _do_pick(self, location: Location) -> None:
        return

    async def _do_place(self, location: Location) -> None:
        self.placing.set()
        await self.may_finish.wait()
        if self.fail_with is not None:
            raise self.fail_with


class _LiftsThePlateOut(TransporterBase):
    """An external arm: its pick is real motion, so the slot really is free."""

    async def initialize(self) -> None:
        return

    @property
    def is_initialized(self) -> bool:
        return True

    async def get_teachpoints(self) -> list[Teachpoint]:
        return []

    async def _do_pick(self, location: Location) -> None:
        return

    async def _do_place(self, location: Location) -> None:
        return


class _Bench:
    def __init__(self, mover: TransporterBase) -> None:
        self.source = Location("source_loc", resource=PlatePad("source_pad"))
        self.target = Location("target_loc", resource=PlatePad("target_pad"))
        self.mover = mover
        self.coordinator = ThreadReservationCoordinator(
            _TwoSlotRegistry([self.source, self.target]), Mock(),
        )
        self.handler = MoveHandler(self.coordinator, Mock(), Mock())

    def can_be_granted_to_the_next_thread(self, location: Location) -> bool:
        return self.coordinator._reservation_manager.can_reserve(
            location.position_id, thread_id=NEXT_IN_LINE,
        )

    async def start_the_move(self, labware: LabwareInstance) -> asyncio.Task[None]:
        service = Mock()
        service.placement.return_value = PlacementState.PRESENT
        service.get.return_value = self.source
        move = MoveAction(labware, self.source, self.target, self.mover)
        reservation = LocationReservation(
            requested_location=self.target, labware=labware,
        )
        reservation.set_location(self.target)
        move.set_reservation(reservation)
        context = ThreadExecutionContext(
            execution_id="exec_1", workflow_name="carry_test",
            thread_id=CARRIER, thread_name="thread1",
            template_name="plate_journey",
        )
        executable = move.executable(
            Mock(), context, service, make_labware_placer(service), self.handler,
        )
        return asyncio.create_task(executable.execute())


async def _bench_with_a_plate(mover: TransporterBase) -> tuple[_Bench, LabwareInstance]:
    bench = _Bench(mover)
    labware = await create_test_labware_instance("plate_1")
    await bench.source.place_labware(labware)
    return bench, labware


class TestTheSlotIsHeldWhileThePlateIsStillInIt:
    async def test_the_next_thread_cannot_be_granted_the_slot_mid_carry(self) -> None:
        bench, labware = await _bench_with_a_plate(_CarriesInOneCall("gripper"))
        mover = bench.mover
        assert isinstance(mover, _CarriesInOneCall)

        move = await bench.start_the_move(labware)
        await asyncio.wait_for(mover.placing.wait(), timeout=5)

        assert bench.source.labware is None, (
            "the record already hands the plate to the jaws -- this is the window"
        )
        assert not bench.can_be_granted_to_the_next_thread(bench.source), (
            "the plate is standing in that slot for the whole of this call"
        )

        mover.may_finish.set()
        await asyncio.wait_for(move, timeout=5)

    async def test_the_slot_is_given_back_once_the_plate_is_at_the_target(self) -> None:
        bench, labware = await _bench_with_a_plate(_CarriesInOneCall("gripper"))
        mover = bench.mover
        assert isinstance(mover, _CarriesInOneCall)

        move = await bench.start_the_move(labware)
        await asyncio.wait_for(mover.placing.wait(), timeout=5)
        mover.may_finish.set()
        await asyncio.wait_for(move, timeout=5)

        assert bench.can_be_granted_to_the_next_thread(bench.source)

    async def test_a_failed_carry_gives_the_slot_back_too(self) -> None:
        """The plate goes back on the slot, so occupancy guards it from here.
        A hold left behind would wedge the slot for the rest of the run."""
        bench, labware = await _bench_with_a_plate(_CarriesInOneCall("gripper"))
        mover = bench.mover
        assert isinstance(mover, _CarriesInOneCall)
        mover.fail_with = RuntimeError("simulated driver refusal")

        move = await bench.start_the_move(labware)
        await asyncio.wait_for(mover.placing.wait(), timeout=5)
        mover.may_finish.set()
        with pytest.raises(RuntimeError, match="simulated driver refusal"):
            await asyncio.wait_for(move, timeout=5)

        assert (
            bench.coordinator._reservation_manager.get_reservation_at(
                bench.source.position_id
            )
            is None
        )

    async def test_an_operator_stop_mid_carry_gives_the_slot_back(self) -> None:
        """The path a stranded slot would come from on a bench.

        A stop or a thread abort cancels the carry where it stands, which is
        exactly when the hold is outstanding. A slot left held by a move that
        no longer exists is unusable for the rest of the run, and nothing an
        operator can do would free it.
        """
        bench, labware = await _bench_with_a_plate(_CarriesInOneCall("gripper"))
        mover = bench.mover
        assert isinstance(mover, _CarriesInOneCall)

        move = await bench.start_the_move(labware)
        await asyncio.wait_for(mover.placing.wait(), timeout=5)
        assert not bench.can_be_granted_to_the_next_thread(bench.source)

        move.cancel()
        with pytest.raises(asyncio.CancelledError):
            await move

        assert bench.can_be_granted_to_the_next_thread(bench.source), (
            "a stop left the slot held by a move that no longer exists"
        )

    async def test_an_arm_that_lifts_the_plate_out_holds_nothing(self) -> None:
        """The other half of the fork. Its pick IS motion, so the slot is
        genuinely free and holding it would stall the next delivery."""
        bench, labware = await _bench_with_a_plate(_LiftsThePlateOut("arm"))

        await asyncio.wait_for(await bench.start_the_move(labware), timeout=5)

        assert bench.can_be_granted_to_the_next_thread(bench.source)


class TestTakingTheHoldNeverWaits:
    async def test_a_slot_somebody_else_holds_is_left_as_it_is(self) -> None:
        """Single-shot on purpose: a mover mid-carry that waited for anything
        could close a wait-for cycle the deadlock detector never sees."""
        bench, labware = await _bench_with_a_plate(_CarriesInOneCall("gripper"))
        squatter = LocationReservation(bench.source, labware)
        await bench.coordinator._reservation_manager.attempt_reservation(
            bench.source.position_id, squatter, thread_id=NEXT_IN_LINE,
        )
        assert squatter.granted.is_set()

        held = await asyncio.wait_for(
            bench.handler.hold_the_source(CARRIER, labware, bench.source),
            timeout=5,
        )

        assert held is None
