"""A thread queued for a device or transporter lock says so.

A lock wait keeps whatever status the thread already had. An arm waiting to
reach into a busy liquid handler stays ``MOVING``, so unless the lock itself is
the published subject a run that has stopped moving shows a thread in flight
with nothing to point at.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cheshire_drivers import SimDriver

from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.tracked_lock import LockWait, TrackedLock, current_lock_wait
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_builders import derive_waiting_for
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.device_call_dispatcher import DeviceCallDispatcher
from orca.workflow_models.actions.move_action import ExecutableMoveAction, MoveAction
from orca.workflow_models.device_handle import ActionRequest
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from tests.test_helpers import (
    create_test_labware_instance,
    create_test_transporter,
    make_labware_placer,
    no_source_hold,
)

# Enough scheduler turns for a queued acquire to reach the lock and park there.
_TURNS_TO_REACH_THE_LOCK = 50


async def _settle() -> None:
    for _ in range(_TURNS_TO_REACH_THE_LOCK):
        await asyncio.sleep(0)


class _HeldDriver(SimDriver):
    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _HeldDevice(Device[_HeldDriver]):
    KIND = "mock"

    def __init__(self, name: str, driver: _HeldDriver, release: asyncio.Event) -> None:
        super().__init__(name, driver, driver)
        self._release = release

    async def shake(self) -> None:
        await self._release.wait()


def _held_device(name: str, release: asyncio.Event) -> _HeldDevice:
    driver = _HeldDriver(name)
    return _HeldDevice(name, driver, release)


def _shake_forever(device: _HeldDevice) -> asyncio.Task[None]:
    """Dispatch a real device command that holds the lock until released."""
    dispatcher = DeviceCallDispatcher(
        device=device,
        interpreter=None,
        operation_log=[],
        action_id="a1",
        thread_id="t1",
        execution_id="e1",
    )
    request = ActionRequest(
        device_name=device.name, command="shake", args=(), kwargs={},
    )
    return asyncio.create_task(dispatcher.dispatch(request, [], []))


def _a_move_context() -> MagicMock:
    context = MagicMock()
    context.execution_id = "exec-lock-wait"
    context.workflow_name = "wf-lock-wait"
    context.thread_id = "t1"
    context.thread_name = "plate_1"
    context.template_name = "tmpl_1"
    return context


def test_a_free_lock_names_no_holder() -> None:
    assert TrackedLock("dev device lock").holder is None


@pytest.mark.asyncio
async def test_a_hold_names_its_purpose_and_gives_the_name_back() -> None:
    lock = TrackedLock("dev device lock")

    async with lock.held_for("aspirate"):
        assert lock.holder == "aspirate"
        assert lock.wait_subject == "dev device lock held by aspirate"

    assert lock.holder is None
    assert not lock.locked()


@pytest.mark.asyncio
async def test_a_hold_taken_by_a_thread_names_the_thread_too() -> None:
    lock = TrackedLock("dev device lock")
    current_lock_wait.set(LockWait(owner="plate_1"))

    async with lock.held_for("aspirate"):
        assert lock.holder == "aspirate (plate_1)"


@pytest.mark.asyncio
async def test_taking_a_free_lock_records_no_wait() -> None:
    lock = TrackedLock("dev device lock")
    slot = LockWait(owner="plate_1")
    current_lock_wait.set(slot)

    async with lock.held_for("aspirate"):
        assert slot.waiting_on is None


@pytest.mark.asyncio
async def test_a_queued_call_publishes_what_it_is_behind_until_it_gets_in() -> None:
    lock = TrackedLock("dev device lock")
    slot = LockWait(owner="plate_1")
    release = asyncio.Event()
    seen_inside: list[str | None] = []

    async def holder() -> None:
        async with lock.held_for("shake"):
            await release.wait()

    async def queued() -> None:
        current_lock_wait.set(slot)
        async with lock.held_for("aspirate"):
            seen_inside.append(slot.waiting_on)

    holding = asyncio.create_task(holder())
    await _settle()
    waiter = asyncio.create_task(queued())
    await _settle()

    assert slot.waiting_on == "dev device lock held by shake"

    release.set()
    await asyncio.wait_for(asyncio.gather(holding, waiter), timeout=5.0)

    # Cleared the moment the wait ended, not merely by the time it finished.
    assert seen_inside == [None]
    assert slot.waiting_on is None


def test_a_wait_too_short_to_matter_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Reached directly: going through ``held_for`` would cost a real second."""
    lock = TrackedLock("dev device lock")

    with caplog.at_level(logging.INFO, logger="orca"):
        lock._report_wait("aspirate", 0.2)

    assert caplog.records == []


def test_one_lock_logs_its_wait_once_per_window(caplog: pytest.LogCaptureFixture) -> None:
    """A retry loop re-enters the same wait every couple of seconds; an
    unthrottled line per acquire buries the rest of the log."""
    lock = TrackedLock("flex_1 device lock")

    with caplog.at_level(logging.INFO, logger="orca"):
        for _ in range(50):
            lock._report_wait("park_gantry (plate_1)", 2.0)

    assert len(caplog.records) == 1
    assert "park_gantry (plate_1) waited 2.0s for flex_1 device lock" in caplog.text


@pytest.mark.asyncio
async def test_a_wait_names_whoever_holds_the_lock_now_not_who_held_it_first() -> None:
    """Three deep, the first holder is long gone by the time anyone looks.

    Naming the hold that was in front when the wait began sends an operator
    after a call that already returned, which is worse than naming nothing.
    """
    lock = TrackedLock("dev device lock")
    third = LockWait(owner="plate_3")
    first_done = asyncio.Event()
    second_done = asyncio.Event()

    async def hold(purpose: str, owner: str, done: asyncio.Event) -> None:
        current_lock_wait.set(LockWait(owner=owner))
        async with lock.held_for(purpose):
            await done.wait()

    async def queued() -> None:
        current_lock_wait.set(third)
        async with lock.held_for("dispense"):
            return None

    first = asyncio.create_task(hold("shake", "plate_1", first_done))
    await _settle()
    second = asyncio.create_task(hold("aspirate", "plate_2", second_done))
    await _settle()
    waiting = asyncio.create_task(queued())
    await _settle()

    assert third.waiting_on == "dev device lock held by shake (plate_1)"

    first_done.set()
    await _settle()

    # Still queued, and now behind the hold that is actually in front.
    assert third.waiting_on == "dev device lock held by aspirate (plate_2)"

    second_done.set()
    await asyncio.wait_for(asyncio.gather(first, second, waiting), timeout=5.0)
    assert third.waiting_on is None


def _build_executing_thread() -> ExecutingLabwareThread:
    location = Location("pad", resource=PlatePad("pad"))
    thread = LabwareThreadInstance(
        labware=LabwareInstance("plate_96", "96_well"),
        start_location=location,
        end_locations=[location],
        run_mode=WorkflowRunMode.PURE_SIM,
    )
    context = MagicMock()
    context.execution_id = "exec-lock-wait"
    context.workflow_name = "wf-lock-wait"
    location_service = MagicMock()
    location_service.get_history.return_value = MagicMock()
    return ExecutingLabwareThread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=location_service,
    )


@pytest.mark.asyncio
async def test_a_started_thread_queued_for_a_device_lock_reports_that_lock() -> None:
    """The whole point: the thread's own snapshot names the lock it is behind.

    ``start`` is what seeds the thread's slot, so the body runs under it here
    rather than the slot being planted by hand.
    """
    release = asyncio.Event()
    device = _held_device("handler_1", release)
    executing = _build_executing_thread()

    async def body() -> None:
        async with device.lock.held_for("prepare_for_place"):
            return None

    holding = _shake_forever(device)
    await _settle()

    with patch.object(executing, "_start_body", AsyncMock(side_effect=body)):
        started = asyncio.create_task(executing.start())
        await _settle()

        assert executing.blocked_on_lock == "handler_1 device lock held by shake"
        assert derive_waiting_for(executing) == "handler_1 device lock held by shake"

        release.set()
        await asyncio.wait_for(asyncio.gather(holding, started), timeout=5.0)

    assert executing.blocked_on_lock is None


@pytest.mark.asyncio
async def test_a_move_queued_for_a_busy_handler_names_the_command_holding_it() -> None:
    """The bench shape: an arm reaching into a handler that is mid-command.

    The move is real (transporter lock -> staging bridge -> device lock) and so
    is the dispatch holding the device, so the subject names the command the
    operator would actually have to wait out.
    """
    release = asyncio.Event()
    device = _held_device("handler_1", release)
    bridge = LabwareStagingBridge("handler_1", device)
    slot = LockWait(owner="plate_1")

    labware = await create_test_labware_instance("plate_1")
    source_pad = PlatePad("source_loc")
    source_pad.initialize_labware(labware)
    source = Location("source_loc", resource=source_pad)
    target = Location("handler_1", resource=bridge)
    transporter = create_test_transporter("arm_1", ["source_loc", "handler_1"])

    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing_move = ExecutableMoveAction(
        status_manager=MagicMock(),
        context=_a_move_context(),
        action=move,
        labware_location_service=MagicMock(),
        labware_placer=make_labware_placer(MagicMock()),
        slot_holder=no_source_hold(),
    )

    async def run_move() -> None:
        current_lock_wait.set(slot)
        await executing_move.execute()

    holding = _shake_forever(device)
    await _settle()

    with (
        patch.object(transporter, "pick", AsyncMock(return_value=None)),
        patch.object(transporter, "place", AsyncMock(return_value=None)),
    ):
        moving = asyncio.create_task(run_move())
        await _settle()

        assert slot.waiting_on == "handler_1 device lock held by shake"

        release.set()
        await asyncio.wait_for(asyncio.gather(holding, moving), timeout=5.0)

    assert slot.waiting_on is None


class TestTheSlotDoesNotStrand:
    """A slot two tasks share must not be left publishing a wait nobody is in."""

    async def test_two_queued_tasks_both_leave_the_slot_empty(self) -> None:
        """The failing shape: one labware thread, two calls queued on one lock.

        Both must be waiting AT THE SAME TIME for the slot to be displaced,
        which is why they queue behind a hold rather than running one after the
        other. If leaving a wait restored what was there before, the second one
        out would put the lock back and the thread would report that wait for
        the rest of its life.
        """
        lock = TrackedLock("flex_1 device lock")
        slot = LockWait(owner="plate_1")
        token = current_lock_wait.set(slot)
        try:
            async def queue_behind() -> None:
                async with lock.held_for("a queued call"):
                    await asyncio.sleep(0)

            async with lock.held_for("the call in front"):
                first = asyncio.create_task(queue_behind())
                second = asyncio.create_task(queue_behind())
                # Both reach their acquire and suspend there, so both have
                # written the slot before either can clear it.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert slot.waiting_on is not None, "neither task queued"

            await asyncio.gather(first, second)

            assert slot.waiting_on is None, (
                f"the thread still reports a wait after both calls returned: "
                f"{slot.waiting_on!r}"
            )
        finally:
            current_lock_wait.reset(token)


class TestAWaitThatNeverEndsStillSaysSo:
    async def test_a_contended_acquire_logs_when_it_starts(self, caplog) -> None:
        """The duration line cannot fire until the lock is in hand, so a hold
        that never returns would otherwise leave no trace at all."""
        lock = TrackedLock("flex_1 device lock")
        slot = LockWait(owner="plate_2")
        token = current_lock_wait.set(slot)
        caplog.set_level(logging.INFO, logger="orca")
        try:
            async with lock.held_for("a long aspirate"):
                waiting = asyncio.create_task(_queue_behind(lock))
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                started = [
                    r.getMessage() for r in caplog.records
                    if "is waiting for" in r.getMessage()
                ]
                assert started, (
                    "a wait still in progress logged nothing; the records were "
                    f"{[r.getMessage() for r in caplog.records]}"
                )
                assert "flex_1 device lock held by a long aspirate" in started[0]
            await waiting
        finally:
            current_lock_wait.reset(token)


async def _queue_behind(lock: TrackedLock) -> None:
    async with lock.held_for("the call that has to wait"):
        return
