"""An arm does not commit to a pick until the target device can take a delivery.

A slot reservation says the site is not claimed. It does not say the device
behind it will let an arm in. Asking only after the pick leaves the arm standing
with a plate in its jaws for as long as the answer takes: on the bench a PF400
held a plate for 34 seconds waiting on a Flex that was mid-hop, and the same
shape scales to an hours-long incubation.

Two things are pinned here. The plate waits on its source instead of in the
jaws, and the arm stays free while it waits, because the wait runs outside the
mover's lock. The gates inside the actuation stay where they are: a device ready
when the arm committed can be busy again by the time it arrives.
"""

import asyncio
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cheshire_drivers.gantry_models import GantryBusyError, ParkGantryRequest
from cheshire_drivers.sims import SimLiquidHandlerDriver

from orca.devices import devices as devices_module
from orca.devices.devices import CannotStepAsideError, LiquidHandler
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.device_error import DeviceUnderExternalControlError
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter_base import (
    MoverAlreadyHoldingError,
    TransporterBase,
)
from orca.runtime.device_factory_context import use_device_factory
from orca.state.ops_history import OpsHistory
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.move_action import (
    ExecutableMoveAction,
    LabwareNotAtSourceError,
    MoveAction,
)
from orca.workflow_models.status_enums import ActionStatus

from tests.test_helpers import (
    no_source_hold,
    _SingleDriverFactory,
    bind_ledger,
    create_test_labware_instance,
    create_test_transporter,
    make_labware_placer,
)

pytestmark = pytest.mark.asyncio


class _Handler(SimLiquidHandlerDriver):
    """The sim handler with one thing added: it refuses to park while ``busy``
    is set, which the sim never does on its own."""

    def __init__(self) -> None:
        super().__init__("flex")
        self.busy = asyncio.Event()
        self.asks = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1
        if self.busy.is_set():
            raise GantryBusyError("FlexHead8 still holds tips")


@pytest.fixture(autouse=True)
def _dont_actually_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry cadence is wall-clock; these pin the waiting, not the clock."""
    monkeypatch.setattr(devices_module, "_STEP_ASIDE_RETRY_SECONDS", 0.01)


def _a_status_manager() -> Mock:
    manager = Mock()
    manager.set_status = Mock()
    return manager


def _a_thread_context() -> Mock:
    context = Mock()
    context.execution_id = "exec_1"
    context.workflow_name = "wf"
    context.thread_id = "thread_1"
    context.thread_name = "thread1"
    context.template_name = "tmpl_1"
    return context


async def _a_move_into(driver: _Handler) -> ExecutableMoveAction:
    """The bench's own shape: an arm reaching from a plain pad into a site on a
    handler that can be asked to move aside."""
    return await _a_move(driver, handler_at_source=False)


async def _a_move_out_of(driver: _Handler) -> ExecutableMoveAction:
    """The mirror: an arm collecting from a handler's site onto a plain pad.

    Both ends are asked before the pick, and only a move each way round pins
    both. Every other test here has the handler at the target.
    """
    return await _a_move(driver, handler_at_source=True)


async def _a_move(driver: _Handler, *, handler_at_source: bool) -> ExecutableMoveAction:
    with use_device_factory(_SingleDriverFactory(driver)):
        handler = LiquidHandler("flex")
    labware = await create_test_labware_instance("plate_1")
    bind_ledger(labware, OpsHistory())
    pad = PlatePad("pad_1")
    site = DeviceDeckSite("flex/C4-slot", handler)
    (site if handler_at_source else pad).initialize_labware(labware)
    ends = (
        Location("flex/C4-slot", resource=site), Location("pad_1", resource=pad),
    )
    source, target = ends if handler_at_source else ends[::-1]
    mover = create_test_transporter("arm", ["pad_1", "flex/C4-slot"])
    move = MoveAction(labware, source, target, mover)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    return ExecutableMoveAction(
        status_manager=_a_status_manager(),
        context=_a_thread_context(),
        action=move,
        labware_location_service=Mock(),
        labware_placer=make_labware_placer(Mock()),
        slot_holder=no_source_hold(),
    )


async def _another_mover_takes(mover: TransporterBase) -> None:
    """Claim the arm the way an unrelated move would, then hand it straight back."""
    async with mover.lock.held_for("an unrelated move"):
        return None


@contextmanager
def _without_actuating(mover: TransporterBase) -> Iterator[AsyncMock]:
    """Stub the two driver actuations, keeping every guard and jaw record real.
    Yields the pick, because when it runs is what most of these are about."""
    with (
        patch.object(mover, "_do_pick", AsyncMock()) as pick,
        patch.object(mover, "_do_place", AsyncMock()),
    ):
        yield pick


async def _wait_for_asks(driver: _Handler, count: int) -> None:
    async def _poll() -> None:
        while driver.asks < count:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=5.0)


async def test_the_arm_does_not_pick_while_the_target_is_refusing() -> None:
    """The whole point: the plate waits on its pad, not in the jaws."""
    driver = _Handler()
    driver.busy.set()
    move = await _a_move_into(driver)

    with _without_actuating(move.transporter) as pick:
        running = asyncio.create_task(move.execute())
        await _wait_for_asks(driver, 2)

        assert pick.await_count == 0, "committed to the pick before asking"
        assert move.source.labware is move.labware, "the plate left its pad"
        assert move.transporter.labware is None, "the jaws took it anyway"

        driver.busy.clear()
        await asyncio.wait_for(running, timeout=5.0)

    assert pick.await_count == 1


async def test_the_arm_is_free_to_others_while_a_move_waits_for_its_target() -> None:
    """Waiting inside the mover's lock would park the whole workcell: the arm is
    the one resource every thread crosses, so a move that waits on one device
    would stop every move that has nothing to do with it."""
    driver = _Handler()
    driver.busy.set()
    move = await _a_move_into(driver)

    with _without_actuating(move.transporter):
        running = asyncio.create_task(move.execute())
        await _wait_for_asks(driver, 2)

        await asyncio.wait_for(_another_mover_takes(move.transporter), timeout=1.0)

        driver.busy.clear()
        await asyncio.wait_for(running, timeout=5.0)


async def test_the_target_is_asked_once_before_committing_and_once_before_placing() -> None:
    """Two gates, not one. The first decides whether to commit; the second is
    the safety check the place itself depends on."""
    driver = _Handler()
    move = await _a_move_into(driver)

    with _without_actuating(move.transporter):
        await move.execute()

    assert driver.asks == 2
    assert move.target.labware is move.labware


async def test_a_target_that_goes_busy_again_still_holds_up_the_place() -> None:
    """The handler is ready when the arm commits and back at work by the time it
    arrives, which is the case the gate before the place exists for."""

    class _ReadyThenBusy(_Handler):
        async def park_gantry(self, request: ParkGantryRequest) -> None:
            self.asks += 1
            if 1 < self.asks <= 3:
                raise GantryBusyError("FlexHead8 still holds tips")

    driver = _ReadyThenBusy()
    move = await _a_move_into(driver)

    with _without_actuating(move.transporter):
        await asyncio.wait_for(move.execute(), timeout=5.0)

    assert driver.asks == 4, "the place went in without waiting out the refusals"
    assert move.target.labware is move.labware


async def test_a_retry_that_already_holds_the_plate_does_not_wait_for_the_target() -> None:
    """An arm with the plate in its jaws owes a place, not a wait. Waiting there
    would be the very stall this is meant to prevent."""
    driver = _Handler()
    move = await _a_move_into(driver)
    await move.source.notify_picked(move.labware, move.transporter)
    await move.transporter.gripper_location.place_labware(move.labware)

    with _without_actuating(move.transporter) as pick:
        await asyncio.wait_for(move.execute(), timeout=5.0)

    assert pick.await_count == 0
    assert driver.asks == 1, "asked before the place only"
    assert move.target.labware is move.labware


async def test_a_target_that_never_frees_up_fails_before_the_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A method that ended with tips on leaves the handler permanently
    unwilling. The move still has to fail, and failing here beats failing after
    the pick: the plate is on its pad and there is nothing to put back."""
    monkeypatch.setattr(devices_module, "STEP_ASIDE_PATIENCE_SECONDS", 0.0)
    driver = _Handler()
    driver.busy.set()
    move = await _a_move_into(driver)

    with _without_actuating(move.transporter) as pick:
        with pytest.raises(CannotStepAsideError):
            await move.execute()

    assert pick.await_count == 0
    assert move.source.labware is move.labware
    assert move.transporter.labware is None
    assert move.status is ActionStatus.ERRORED


@pytest.mark.parametrize("how", ["operator hold", "gateway command"])
async def test_a_device_an_operator_has_taken_is_not_asked_to_park(how: str) -> None:
    """External control is a flag, not a lock, so nothing but this check stops a
    park. An operator takes a device to keep workflow moves out while they work,
    sometimes with their hands in the machine; a gantry moving then is the whole
    thing they took it to prevent.

    Both ways in are covered because both feed one property: an operator's
    standing hold, and the flag the gateway sets around a single command.
    """
    driver = _Handler()
    move = await _a_move_into(driver)
    site = move.target.resource
    assert isinstance(site, DeviceDeckSite)
    if how == "operator hold":
        site.device.hold_external_control("operator is in the deck")
    else:
        site.device.take_external_control()

    with _without_actuating(move.transporter) as pick:
        with pytest.raises(DeviceUnderExternalControlError):
            await move.execute()

    assert driver.asks == 0
    assert pick.await_count == 0


async def test_a_move_behind_another_still_waits_for_its_own_target() -> None:
    """The jaws are full of somebody else's plate for the whole of every move in
    flight. Skipping the wait on that would skip it for every move queued behind
    one, which is exactly the contention this exists for."""
    driver = _Handler()
    move = await _a_move_into(driver)
    stranger = await create_test_labware_instance("plate_2")
    bind_ledger(stranger, OpsHistory())
    await move.transporter.gripper_location.place_labware(stranger)

    with _without_actuating(move.transporter):
        with pytest.raises(MoverAlreadyHoldingError):
            await move.execute()

    # One ask, not two: the pre-flight asked the target, and the pick raised
    # before the gate before the place. The source is a plain pad and asks
    # nothing. Zero is the old behaviour, which skipped the wait entirely.
    assert driver.asks == 1, "skipped the wait because the jaws were not ours"


async def test_a_plate_that_left_its_source_is_refused_before_anything_parks() -> None:
    """Parking two handlers, twice, for a plate that is not there wastes minutes
    before the refusal that tells the operator what to do."""
    driver = _Handler()
    move = await _a_move_into(driver)
    await move.source.notify_picked(move.labware, move.transporter)

    with _without_actuating(move.transporter):
        with pytest.raises(LabwareNotAtSourceError):
            await move.execute()

    assert driver.asks == 0


async def test_the_arm_does_not_pick_while_the_SOURCE_is_refusing() -> None:
    """Both ends, not just the target.

    A handler the arm is collecting FROM has to move off its own deck first, and
    waiting for that with the mover's lock held parks every other move on that
    arm for a device none of them is going near. Nothing else here has the
    handler at the source, so nothing else would notice that ask going missing.
    """
    driver = _Handler()
    driver.busy.set()
    move = await _a_move_out_of(driver)

    with _without_actuating(move.transporter) as pick:
        running = asyncio.create_task(move.execute())
        await _wait_for_asks(driver, 2)

        assert pick.await_count == 0, "reached into a deck that had not moved"
        await asyncio.wait_for(_another_mover_takes(move.transporter), timeout=1.0)

        driver.busy.clear()
        await asyncio.wait_for(running, timeout=5.0)

    assert pick.await_count == 1
