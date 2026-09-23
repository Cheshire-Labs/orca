"""A plate that needs somewhere to land beats a plate nobody has placed yet.

Ruled from the bench, 2026-09-03, watching a two-group run on a real
Flex/PF400. One pad served both the hand-in and the way home. The first plate
finished, staged next to the pad, and asked for it. The pad was empty, and the
claim on it belonged to a thread waiting for a person to place the SECOND
plate. So a real plate waited on an expectation, and the expectation waited on
an operator who had been given no reason to act. Neither moved.

Two halves, both here:

1. The returning plate takes the pad. The operator wait yields its claim and
   asks for it again once the pad is genuinely free.
2. A register into a pad with a real plate inbound is refused, and the refusal
   names the plate that is coming and says the pad frees when it lands and the
   operator removes it.

The order terminates: the operator removes the returned plate, then places the
next one. The order it replaces terminates only if the operator happens to
place first, which is what nothing was telling them to do.

``test_a_pending_placement_claims_its_spot`` holds the other side of the same
rule: an expectation still claims its spot, and still keeps it against
everything that is not a plate looking for somewhere to land.
"""

import asyncio

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.transporter import Transporter
from orca.runtime.runtime_interface import LocationReservedError
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    ReservationPriority,
)
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.test_a_pending_placement_claims_its_spot import (
    Bench,
    _abandon,
    _build_bench,
    _claim_holder,
    _collect_finished_plates,
    _submit,
    _wait_for_a_parked_wait,
)
from tests.test_helpers import (
    create_test_teachpoints,
    seeded_teachpoint_service,
    wait_until,
)


class ArmHeldOverThePad(Transporter):
    """A transporter whose place into one position waits to be let go.

    A move that has reserved its target but not yet put the plate down is a
    one-tick window on a real bench. Holding the arm there is what makes it a
    state a test can stand in and ask questions of.
    """

    def __init__(
        self, name: str, position_ids: list[str], hold_at: str,
    ) -> None:
        super().__init__(
            name,
            teachpoint_store=seeded_teachpoint_service(
                create_test_teachpoints(position_ids),
            ),
        )
        self._hold_at = hold_at
        self.let_go = asyncio.Event()

    async def _do_place(self, location: Location) -> None:
        if location.position_id == self._hold_at:
            await self.let_go.wait()
        await super()._do_place(location)


def _thread_holding(bench: Bench, execution_id: str, labware_id: str) -> str:
    """The id of the thread carrying this labware."""
    match = [
        t.id for t in bench.runtime.list_threads(execution_id)
        if t.labware_id == labware_id
    ]
    assert len(match) == 1, f"expected one thread for {labware_id}, got {match}"
    return match[0]


def _status_of(bench: Bench, execution_id: str, thread_id: str) -> str:
    return next(
        t.status for t in bench.runtime.list_threads(execution_id)
        if t.id == thread_id
    )


def _pad_holds(bench: Bench, position_id: str) -> LabwareInstance | None:
    return bench.runtime.system.system_map.get_location(position_id).labware


class TestThePlateComingHomeWins:

    async def test_a_returning_plate_takes_the_pad_an_expectation_is_holding(
        self,
    ) -> None:
        """The bench case. Two lineages share one pad, which is both where a
        plate is handed in and where it goes back.

        The operator places the first plate and is asked for nothing else. The
        second lineage's wait takes the pad the moment the first plate leaves
        it. The first plate then finishes and needs that same pad to go home.

        It gets it. Before this rule the move sat at AWAITING_MOVE_RESERVATION
        for as long as the operator did nothing, and the operator had no reason
        to do anything.
        """
        bench = await _build_bench(end_pad="pad1")
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench, groups=2)
            await _wait_for_a_parked_wait(bench, execution_id, count=2)

            placed = await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            traveller = _thread_holding(bench, execution_id, placed.id)

            await wait_until(
                lambda: (
                    _status_of(bench, execution_id, traveller)
                    == LabwareThreadStatus.AWAITING_MANUAL_REMOVE.value
                ),
                timeout=60.0,
                message="the first plate never got home to the shared pad",
            )
            held = _pad_holds(bench, "pad1")
            assert held is not None and held.id == placed.id

            still_waiting = [
                t.id for t in bench.runtime.list_threads(execution_id)
                if t.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.value
            ]
            assert still_waiting, (
                "the second lineage stopped waiting for its plate; the pad was "
                "taken from it, not handed back"
            )

            await _abandon(bench, execution_id)
        finally:
            await bench.runtime.shutdown()

    async def test_the_operator_can_finish_the_sequence_the_rule_starts(
        self,
    ) -> None:
        """The reverse starvation, and why it is not one.

        Losing the pad costs the waiting lineage the wait it takes an operator
        to remove the plate that landed. Then the claim comes back and the
        placement the operator was asked for goes through. Every step is
        something a person can do without being told anything new.
        """
        bench = await _build_bench(end_pad="pad1")
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench, groups=2)
            await _wait_for_a_parked_wait(bench, execution_id, count=2)
            placed = await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            traveller = _thread_holding(bench, execution_id, placed.id)
            await wait_until(
                lambda: (
                    _status_of(bench, execution_id, traveller)
                    == LabwareThreadStatus.AWAITING_MANUAL_REMOVE.value
                ),
                timeout=60.0,
                message="the first plate never got home to the shared pad",
            )

            await bench.runtime.labware.discharge_labware(placed.id)

            waiting = [
                t.id for t in bench.runtime.list_threads(execution_id)
                if t.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.value
            ]
            await wait_until(
                lambda: _claim_holder(bench) in waiting,
                timeout=30.0,
                message="the waiting lineage never got the pad back",
            )

            second = await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            assert second.current_location == "pad1"

            # And the whole shape runs to the end in sim: the second plate
            # goes out to the shaker and comes back to the pad an operator
            # cleared by hand, which is the arm's own world agreeing with the
            # engine about a position neither of them emptied.
            statuses = await _collect_finished_plates(bench, execution_id)
            assert set(statuses.values()) == {"COMPLETED"}, statuses
        finally:
            await bench.runtime.shutdown()


class TestTheRefusalSaysWhatToWaitFor:

    async def _bench_with_a_held_arm(self) -> tuple[Bench, ArmHeldOverThePad]:
        arm = ArmHeldOverThePad("robot1", ["shaker1", "pad1", "pad2"], "pad1")
        bench = await _build_bench(end_pad="pad1", arm=arm)
        await bench.runtime.start()
        return bench, arm

    async def _hold_a_plate_over_the_pad(
        self, bench: Bench,
    ) -> tuple[str, LabwareInstance]:
        """Run one plate to the point where it has reserved pad1 and is about
        to be put down on it. Returns the execution and the plate."""
        execution_id = await _submit(bench)
        await _wait_for_a_parked_wait(bench, execution_id)
        placed = await bench.runtime.labware.register(
            "plate_96", location="pad1", confirm=True,
        )
        traveller = _thread_holding(bench, execution_id, placed.id)
        await wait_until(
            lambda: _claim_holder(bench) == traveller and _pad_holds(bench, "pad1") is None,
            timeout=60.0,
            message="the plate never reserved the pad it was heading back to",
        )
        instance = next(
            lw for lw in bench.runtime.system.labwares if lw.id == placed.id
        )
        return execution_id, instance

    async def test_a_register_is_refused_and_names_the_plate_on_its_way(
        self,
    ) -> None:
        """"Reserved by thread 0b1a9c8c" is not an instruction. The operator
        needs the plate's name, where it is coming from, and the one thing that
        frees the pad."""
        bench, arm = await self._bench_with_a_held_arm()
        try:
            execution_id, inbound = await self._hold_a_plate_over_the_pad(bench)

            try:
                await bench.runtime.labware.register(
                    "plate_96", location="pad1", confirm=True,
                )
            except LocationReservedError as refusal:
                assert refusal.inbound_labware == inbound.name
                message = str(refusal)
                assert inbound.name in message
                assert "remove" in message.lower()
                assert "take your labware back off" in message.lower(), (
                    "this refusal reaches someone who has already put the "
                    "plate down, and the engine cannot see labware nobody "
                    "registered"
                )
            else:
                raise AssertionError(
                    "a register into a pad with a plate inbound was allowed"
                )

            arm.let_go.set()
            await _abandon(bench, execution_id)
        finally:
            arm.let_go.set()
            await bench.runtime.shutdown()

    async def test_edit_and_reset_location_refuse_the_same_way(self) -> None:
        """Both verbs that state a position reach the same state as register,
        so both owe the operator the same answer."""
        bench, arm = await self._bench_with_a_held_arm()
        try:
            execution_id, inbound = await self._hold_a_plate_over_the_pad(bench)
            stray = await bench.runtime.labware.register(
                "plate_96", location="pad2", confirm=True,
            )

            for verb in (
                bench.runtime.labware.edit_location,
                bench.runtime.labware.reset_location,
            ):
                try:
                    await verb(stray.id, "pad1", reason="test", confirm=True)
                except LocationReservedError as refusal:
                    assert refusal.inbound_labware == inbound.name, verb
                else:
                    raise AssertionError(f"{verb} was allowed onto a claimed pad")

            arm.let_go.set()
            await _abandon(bench, execution_id)
        finally:
            arm.let_go.set()
            await bench.runtime.shutdown()


class TestAHoldSaysWhatItIsFor:

    async def test_the_listing_tells_a_waiting_hold_from_an_arriving_plate(
        self,
    ) -> None:
        """A held position reads the same either way on a listing, and the two
        want opposite things from whoever wants the position next. An operator
        surface that only knows "pad1 is held" invites a placement the engine is
        about to refuse."""
        arm = ArmHeldOverThePad("robot1", ["shaker1", "pad1", "pad2"], "pad1")
        bench = await _build_bench(end_pad="pad1", arm=arm)
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench)
            await _wait_for_a_parked_wait(bench, execution_id)

            waiting = next(
                r for r in bench.runtime.list_reservations(execution_id)
                if r.position_id == "pad1"
            )
            assert waiting.awaiting_operator is True
            assert waiting.arriving is False
            assert waiting.labware_name is not None

            placed = await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            traveller = _thread_holding(bench, execution_id, placed.id)
            await wait_until(
                lambda: _claim_holder(bench) == traveller
                and _pad_holds(bench, "pad1") is None,
                timeout=60.0,
                message="the plate never reserved the pad it was heading back to",
            )

            inbound = next(
                r for r in bench.runtime.list_reservations(execution_id)
                if r.position_id == "pad1"
            )
            assert inbound.awaiting_operator is False
            assert inbound.arriving is True, (
                "a surface reading 'a plate is landing here' needs the hold to "
                "say so, not just to be carrying labware"
            )
            assert inbound.labware_name == placed.name

            arm.let_go.set()
            await _abandon(bench, execution_id)
        finally:
            arm.let_go.set()
            await bench.runtime.shutdown()


class TestNothingOutranksAPlateThatIsAlreadyThere:

    async def test_an_occupied_pad_is_refused_however_high_the_requester_ranks(
        self,
    ) -> None:
        """Tier 1. Displacing an operator wait lets a request past the holder
        check, and it must still stop at the plate standing on the spot --
        otherwise the rule that fixes one collision buys another."""
        from orca.system.reservation_manager.reservation_manager import (
            LocationReservationManager,
        )
        from orca.system.resource_registry import ResourceRegistry
        from orca.system.system_map import SystemMap

        system_map = SystemMap(ResourceRegistry())
        pad = Location("pad1")
        await system_map.add_location(pad)
        manager = LocationReservationManager(system_map)

        expected = LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat")
        claim = LocationReservation(
            pad, expected, priority=ReservationPriority.AWAITING_OPERATOR,
        )
        await manager.attempt_reservation("pad1", claim, thread_id="waiting")
        assert claim.granted.is_set()

        resident = LabwareInstance("plate_3", "corning_96_wellplate_360ul_flat")
        pad.initialize_labware(resident)

        inbound = LabwareInstance("plate_1", "corning_96_wellplate_360ul_flat")
        assert manager.can_reserve(
            "pad1", thread_id="coming-home", requesting_labware_id=inbound.id,
        ) is False, "a plate standing on the pad was displaced by a tier check"

    async def test_the_detector_does_not_invent_a_wait_the_gate_would_not(
        self,
    ) -> None:
        """The detector works out who a request waits for by asking what the
        gate asks. A plate on its way to a pad an operator wait is holding is
        granted, so nothing is waiting for that thread, and counting it would
        have the detector hunting a cycle through a thread that is not in one.
        """
        from unittest.mock import MagicMock

        from orca.system.reservation_manager.deadlock_manager import (
            DeadlockStarvationRegistry,
            ThreadDeadlockDetector,
        )

        pad = Location("pad1")
        held: dict[str, LocationReservation] = {}
        detector = ThreadDeadlockDetector(
            thread_registry=MagicMock(),
            starvation_registry=DeadlockStarvationRegistry(),
            reservation_at=held.get,
        )

        claim = LocationReservation(
            pad, LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat"),
            priority=ReservationPriority.AWAITING_OPERATOR,
        )
        claim.thread_id = "waiting"
        held["pad1"] = claim

        coming_home = LocationReservation(
            pad, LabwareInstance("plate_1", "corning_96_wellplate_360ul_flat"),
            priority=ReservationPriority.MOVE_TARGET,
        )
        assert detector._blocker_thread_ids(coming_home, {}, "going-home") == set()

        # Everything that is not a plate arriving still waits its turn, so the
        # detector still has a cycle to find through it.
        an_action = LocationReservation(
            pad, LabwareInstance("plate_3", "corning_96_wellplate_360ul_flat"),
        )
        assert detector._blocker_thread_ids(an_action, {}, "someone-else") == {
            "waiting",
        }

    async def test_an_operator_wait_keeps_a_carriage_against_everything(
        self,
    ) -> None:
        """A single-carriage station is refused while a sibling station is
        claimed, and a plate arriving does not change that.

        Granting over a sibling would leave the sibling's claim standing --
        only the holder at the requested position is displaced -- so a person
        would still be holding an instruction to fill a carriage a plate is
        landing on, and that placement would not be refused.
        """
        from orca.system.reservation_manager.reservation_manager import (
            LocationReservationManager,
        )
        from orca.system.resource_registry import ResourceRegistry
        from orca.system.system_map import SystemMap

        system_map = SystemMap(ResourceRegistry())
        station_a = Location("carriage/a")
        station_b = Location("carriage/b")
        await system_map.add_location(station_a)
        await system_map.add_location(station_b)
        siblings = {"carriage/a": [station_b], "carriage/b": [station_a]}
        manager = LocationReservationManager(
            system_map, exclusion_siblings_of=lambda p: siblings.get(p, []),
        )

        claim = LocationReservation(
            station_b,
            LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat"),
            priority=ReservationPriority.AWAITING_OPERATOR,
        )
        await manager.attempt_reservation("carriage/b", claim, thread_id="waiting")
        assert claim.granted.is_set()

        inbound = LabwareInstance("plate_1", "corning_96_wellplate_360ul_flat")
        assert manager.can_reserve(
            "carriage/a", thread_id="coming-home",
            requesting_labware_id=inbound.id,
            requesting_priority=ReservationPriority.MOVE_TARGET,
        ) is False
        assert not claim.is_displaced

    async def test_one_operator_wait_does_not_displace_another(self) -> None:
        """Two expectations on one pad rank the same, so the first keeps it.
        The second operator instruction is the one that has to wait."""
        from orca.system.reservation_manager.reservation_manager import (
            LocationReservationManager,
        )
        from orca.system.resource_registry import ResourceRegistry
        from orca.system.system_map import SystemMap

        system_map = SystemMap(ResourceRegistry())
        pad = Location("pad1")
        await system_map.add_location(pad)
        manager = LocationReservationManager(system_map)

        first = LabwareInstance("plate_1", "corning_96_wellplate_360ul_flat")
        held = LocationReservation(
            pad, first, priority=ReservationPriority.AWAITING_OPERATOR,
        )
        await manager.attempt_reservation("pad1", held, thread_id="first")

        second = LabwareInstance("plate_2", "corning_96_wellplate_360ul_flat")
        assert manager.can_reserve(
            "pad1", thread_id="second", requesting_labware_id=second.id,
            requesting_priority=ReservationPriority.AWAITING_OPERATOR,
        ) is False
