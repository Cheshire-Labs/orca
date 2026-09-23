"""A move the driver refuses must not leave the plate recorded in the jaws.

A liquid handler's own gripper carries a plate with ONE driver call, issued at
place time. Its pick actuates nothing; it only notes where the plate is coming
from. The move recorded the plate into the jaws the instant that pick returned,
so a refused ``move_plate`` left a hold for a pick that never happened while the
plate sat on its slot untouched.

Three things went wrong out of that one record, and each looked like its own
problem: the next plate to want the gripper was refused because the jaws read
full, a deck reconcile dropped a labware that sits at no site so the retry died
on "resource not found", and the compare after it said the two models agreed.
"""

from typing import Awaitable, Callable
from unittest.mock import Mock

import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import MovePlateRequest
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers.sims import RecordedCall
from cheshire_drivers.teachpoints import Teachpoint
from orca.devices.devices import LiquidHandler
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import PlacementState
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter_base import TransporterBase
from orca.sdk.build import SystemBuild
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.system_interface import DeckConflictReason
from orca.workflow_models.actions.move_action import MoveAction
from tests.test_deck_gripper_hop import (
    ARM_TAUGHT_SITE,
    WORKING_SLOT,
    build_single_plate_system,
)
from tests.test_helpers import create_test_labware_instance, make_labware_placer, no_source_hold


class RefusingPlateMover(RecordingLiquidHandlerDriver):
    """Refuses every ``move_plate``, the way a driver refuses a move it cannot
    make. Nothing on the deck is touched, so the plate stays where it was."""

    async def move_plate(self, request: MovePlateRequest) -> None:
        self.calls.append(RecordedCall(method="move_plate", args=request.model_dump()))
        raise RuntimeError("simulated driver refusal")


class _ArmThatWillNotLetGo(TransporterBase):
    """An external arm: its pick is real motion, and its place refuses."""

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
        raise RuntimeError("the arm would not let go")


def _service_placing_at(location: Location) -> Mock:
    service = Mock()
    service.placement.return_value = PlacementState.PRESENT
    service.get.return_value = location
    return service


def _status_manager() -> Mock:
    sm = Mock()
    sm.set_status = Mock()
    return sm


def _thread_context() -> Mock:
    ctx = Mock()
    ctx.execution_id = "exec_1"
    ctx.workflow_name = "hop_test"
    ctx.thread_id = "thread_1"
    ctx.thread_name = "thread1"
    ctx.template_name = "plate_journey"
    return ctx


class _Bench:
    def __init__(self, build: SystemBuild, labware: LabwareInstance) -> None:
        self.build = build
        self.labware = labware
        self.gripper: TransporterBase = next(
            m for m in build.system.movers if m.name == "lh/gripper"
        )
        self.source = build.system.system_map.get_location(f"lh/{ARM_TAUGHT_SITE}")
        self.target = build.system.system_map.get_location(f"lh/{WORKING_SLOT}")

    @property
    def device_location(self) -> Location:
        return self.build.system.system_map.get_location("lh")

    async def hop(self) -> None:
        """Run one gripper hop the way a thread runs it: through the move action."""
        move = MoveAction(self.labware, self.source, self.target, self.gripper)
        reservation = LocationReservation(
            requested_location=self.target, labware=self.labware,
        )
        reservation.set_location(self.target)
        move.set_reservation(reservation)
        await move.executable(
            _status_manager(),
            _thread_context(),
            self.build.system.labware_location_service,
            self.build.system.labware_placer,
            no_source_hold(),
        ).execute()

    async def strand_the_plate_in_the_jaws(self) -> None:
        """Leave the plate held the way an interrupted move does."""
        await self.gripper.pick(self.source)
        await self.build.system.labware_placer.picked_up(
            self.labware, self.gripper.gripper_location,
        )
        await self.source.notify_picked(self.labware, self.gripper)

    def ledger_position(self) -> str:
        return self.build.system.labware_location_service.get(
            self.labware
        ).position_id


async def _bench(driver: RecordingLiquidHandlerDriver) -> _Bench:
    build = await build_single_plate_system(driver)
    lh = next(d for d in build.system.devices if isinstance(d, LiquidHandler))
    await lh.deck_world_layout()
    labware = await build.system.get_labware_template("sample_plate").create_instance()
    await labware.enter_record(build.system.labware_contents)
    bench = _Bench(build, labware)
    await build.system.labware_placer.place(labware, bench.source)
    return bench


def _refusing() -> RefusingPlateMover:
    return RefusingPlateMover(ChatterboxLiquidHandlerDriver(num_channels=8))


def _plain() -> RecordingLiquidHandlerDriver:
    return RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))


@pytest.mark.timeout(30)
async def test_a_refused_move_leaves_the_plate_on_the_slot_it_started_from() -> None:
    bench = await _bench(_refusing())

    with pytest.raises(RuntimeError, match="simulated driver refusal"):
        await bench.hop()

    assert bench.gripper.labware is None, (
        "the pick actuated nothing, so the jaws never held it"
    )
    assert bench.source.labware is bench.labware, "the plate never left its slot"
    assert bench.ledger_position() == bench.source.position_id
    assert (
        bench.build.system.labware_location_service.placement(bench.labware)
        is PlacementState.PRESENT
    )


@pytest.mark.timeout(30)
async def test_a_refused_move_leaves_the_gripper_free_for_the_next_plate() -> None:
    """The symptom that reached the operator: a SECOND plate's first hop was
    refused before it reached the driver, because the jaws read full."""
    bench = await _bench(_refusing())
    with pytest.raises(RuntimeError, match="simulated driver refusal"):
        await bench.hop()

    other = await bench.build.system.get_labware_template(
        "sample_plate"
    ).create_instance()
    await other.enter_record(bench.build.system.labware_contents)
    await bench.target.place_labware(other)

    # A jaws record that reads full makes this raise MoverAlreadyHoldingError,
    # a refusal decided before anything reaches the driver.
    await bench.gripper.pick(bench.target)

    assert bench.gripper.picked_from_position_id == bench.target.position_id


@pytest.mark.timeout(30)
async def test_anything_failing_between_the_pick_record_and_the_place_puts_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window is wider than the driver call. Telling the source it lost the
    plate, and clearing the target's deck for the arriving one, are both device
    calls that run after the jaws record is written and can raise."""
    bench = await _bench(_plain())

    async def refuse(labware: LabwareInstance, mover: TransporterBase) -> None:
        raise RuntimeError("the device would not let go")

    monkeypatch.setattr(bench.source, "notify_picked", refuse)

    with pytest.raises(RuntimeError, match="would not let go"):
        await bench.hop()

    assert bench.gripper.labware is None
    assert bench.source.labware is bench.labware
    assert bench.ledger_position() == bench.source.position_id


@pytest.mark.timeout(30)
async def test_an_arm_that_really_lifted_the_plate_goes_on_holding_it() -> None:
    """The other half of the fork. An external arm's pick IS motion, so a place
    that fails leaves the plate in its jaws for real, and putting the record
    back on the source would be the same lie pointing the other way."""
    source = Location("source_loc", resource=PlatePad("source_pad"))
    target = Location("target_loc", resource=PlatePad("target_pad"))
    arm = _ArmThatWillNotLetGo("arm")
    labware = await create_test_labware_instance("plate_1")
    await source.place_labware(labware)
    service = _service_placing_at(source)
    move = MoveAction(labware, source, target, arm)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)

    with pytest.raises(RuntimeError, match="would not let go"):
        await move.executable(
            _status_manager(), _thread_context(),
            service, make_labware_placer(service), no_source_hold(),
        ).execute()

    assert arm.labware is labware, "the plate really is in the jaws"
    assert source.labware is None, "and really is not on the slot any more"


@pytest.mark.timeout(30)
async def test_the_retry_of_a_refused_move_reissues_it_from_the_same_source() -> None:
    """Recovery still has to work: putting the plate back leaves it where the
    next attempt looks for it, and that attempt sends the identical call."""
    driver = _refusing()
    bench = await _bench(driver)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="simulated driver refusal"):
            await bench.hop()

    moves = [c for c in driver.calls if c.method == "move_plate"]
    assert len(moves) == 2, f"both attempts must reach the driver; got {moves}"
    assert [m.args["from_position"] for m in moves] == [ARM_TAUGHT_SITE] * 2
    assert [m.args["to_position"] for m in moves] == [WORKING_SLOT] * 2


@pytest.mark.timeout(30)
async def test_a_reconcile_after_a_refused_move_still_declares_the_plate() -> None:
    """The reconcile rebuilds the deck from the ledger, so a plate the ledger
    holds at no site is dropped from the driver and the retry then names a
    plate the driver does not have. With no phantom hold left there is nothing
    to drop."""
    driver = _refusing()
    bench = await _bench(driver)
    with pytest.raises(RuntimeError, match="simulated driver refusal"):
        await bench.hop()

    await bench.build.system.reconcile_lh_deck_occupancy(bench.device_location)

    pushes = [c for c in driver.calls if c.method == "reconcile_deck_occupancy"]
    assert pushes, "the reconcile must reach the driver"
    declared = [r["name"] for r in pushes[-1].args["resources"]]
    assert declared == [bench.labware.name], (
        f"the plate must survive the reconcile; got {declared}"
    )


@pytest.mark.timeout(30)
async def test_a_plate_really_in_the_jaws_is_projected_at_the_site_it_left() -> None:
    """The honest case the root fix does not remove: a move that died mid-flight
    leaves the plate held for real. A deck addresses labware by site and the jaws
    are not one, so the reconcile projects it at the site the move began at,
    which is what the driver's own interrupted-move report names too. Dropping
    it instead loses the plate from the driver, and the retry dies on it."""
    driver = _plain()
    bench = await _bench(driver)
    await bench.strand_the_plate_in_the_jaws()

    await bench.build.system.reconcile_lh_deck_occupancy(bench.device_location)

    pushes = [c for c in driver.calls if c.method == "reconcile_deck_occupancy"]
    declared = {r["name"]: r for r in pushes[-1].args["resources"]}
    assert bench.labware.name in declared, (
        f"a held plate must stay on the driver deck; got {list(declared)}"
    )
    carrier, _, index = ARM_TAUGHT_SITE.rpartition("-")
    entry = declared[bench.labware.name]
    assert (entry["parent_id"], entry["site_index"]) == (carrier, int(index)), (
        f"it must be declared at the site the hop began at; got {entry}"
    )


@pytest.mark.timeout(30)
async def test_a_plate_in_the_jaws_is_never_reported_as_agreement() -> None:
    """``compare_deck`` walked deck slots only, so a plate the ledger held in the
    jaws was on neither side of the comparison and it answered `agrees`."""
    bench = await _bench(_plain())
    await bench.strand_the_plate_in_the_jaws()

    comparison = await bench.build.system.compare_lh_deck_occupancy(
        bench.device_location
    )

    assert comparison is not None
    held = [
        c for c in comparison.disagreements
        if c.reason is DeckConflictReason.HELD_BY_GRIPPER
    ]
    assert [c.labware_name for c in held] == [bench.labware.name], (
        f"the held plate must be a disagreement; got {comparison.disagreements}"
    )
    assert not any(
        c.reason is DeckConflictReason.UNKNOWN_TO_LEDGER
        for c in comparison.disagreements
    ), "the ledger knows this plate; it is in the jaws, not unknown to it"


class _RefusingAfterTheSourceRefills(RecordingLiquidHandlerDriver):
    """Refuses the move, but only after the arm has dropped the next plate on
    the slot this hop started from."""

    def __init__(
        self, inner: ChatterboxLiquidHandlerDriver, refill: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(inner)
        self._refill = refill

    async def move_plate(self, request: MovePlateRequest) -> None:
        self.calls.append(RecordedCall(method="move_plate", args=request.model_dump()))
        await self._refill()
        raise RuntimeError("simulated driver refusal")


@pytest.mark.timeout(30)
async def test_a_source_that_refilled_keeps_the_hold_and_the_drivers_own_error() -> None:
    """A handoff site takes the next plate the moment this one is picked, so the
    put-back can find the slot taken. There is nowhere to put the plate, so the
    jaws record stands -- but the operator must still be told what the driver
    actually said, not that a slot was occupied."""
    holder: dict[str, _Bench] = {}

    async def refill() -> None:
        bench = holder["bench"]
        newcomer = await bench.build.system.get_labware_template(
            "sample_plate"
        ).create_instance()
        await newcomer.enter_record(bench.build.system.labware_contents)
        await bench.source.place_labware(newcomer)

    driver = _RefusingAfterTheSourceRefills(
        ChatterboxLiquidHandlerDriver(num_channels=8), refill,
    )
    bench = await _bench(driver)
    holder["bench"] = bench

    with pytest.raises(RuntimeError, match="simulated driver refusal"):
        await bench.hop()

    assert bench.gripper.labware is bench.labware, (
        "with the slot taken the plate has nowhere to go back to, so the hold "
        "is the honest record"
    )
