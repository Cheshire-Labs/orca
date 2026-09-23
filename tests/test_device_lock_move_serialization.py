"""The move/prepare path must serialize on the device lock with the
action-dispatch path.

Two orca code paths drive commands to one physical device: the action
dispatcher (``DeviceCallDispatcher`` holds ``device.lock`` around the
driver call) and the transporter move/prepare path (``LabwareStagingBridge``
calls the device's ``_do_prepare_*`` / ``_do_notify_*`` hooks). If the move
path does not also hold ``device.lock``, the two overlap and a downstream
gateway with a fail-fast per-device lock rejects the second command -- the
``journey_probe`` ``DeviceLockedError`` wedge.
"""
from tests.mock import EXTERNAL_MOVER

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cheshire_drivers import SimDriver

from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
)
from orca.workflow_models.actions.device_call_dispatcher import (
    DeviceCallDispatcher,
)
from orca.workflow_models.actions.move_action import (
    ExecutableMoveAction,
    MoveAction,
)
from orca.workflow_models.device_handle import ActionRequest
from orca.workflow_models.status_enums import ActionStatus
from tests.test_helpers import (
    no_source_hold,
    make_labware_placer,
    create_test_labware_instance,
    create_test_transporter,
)


class _ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.total_entries = 0

    async def enter(self) -> None:
        self.active += 1
        self.total_entries += 1
        self.max_active = max(self.max_active, self.active)
        # Pump the loop (turns, not wall-clock) to widen the window: an
        # unserialized second path reaches enter() and pushes active past 1.
        for _ in range(50):
            await asyncio.sleep(0)
        self.active -= 1


class _InstrDriver(SimDriver):
    """Driver whose hardware hooks record concurrent entry."""

    def __init__(self, name: str, tracker: _ConcurrencyTracker) -> None:
        super().__init__(name)
        self._tracker = tracker

    async def open(self) -> None:
        await self._tracker.enter()

    async def close(self) -> None:
        await self._tracker.enter()


class _InstrDevice(Device[_InstrDriver]):
    KIND = "mock"

    def __init__(
        self,
        name: str,
        driver: _InstrDriver,
        sim_driver: _InstrDriver,
        tracker: _ConcurrencyTracker | None = None,
        sim_override: WorkflowRunMode | None = None,
    ) -> None:
        super().__init__(name, driver, sim_driver, sim_override=sim_override)
        self._tracker = tracker

    async def busy_op(self) -> None:
        """A dispatchable device command, instrumented like a real action."""
        assert self._tracker is not None
        await self._tracker.enter()


def _mock_status_manager() -> Mock:
    sm = Mock()
    sm.set_status = Mock()
    return sm


def _mock_thread_context() -> Mock:
    ctx = Mock()
    ctx.execution_id = "exec_1"
    ctx.workflow_name = "wf"
    ctx.thread_id = "thread_1"
    ctx.thread_name = "thread1"
    ctx.template_name = "tmpl_1"
    return ctx


@pytest.mark.asyncio
async def test_move_prepare_serializes_with_device_lock() -> None:
    tracker = _ConcurrencyTracker()
    driver = _InstrDriver("dev", tracker)
    device = _InstrDevice("dev", driver, driver)
    bridge = LabwareStagingBridge("dev", device)
    labware = LabwareInstance("plate", "96_well")

    async def dispatch_holds_lock() -> None:
        # Models DeviceCallDispatcher: holds device.lock around a driver call.
        async with device.lock.held_for("busy_op"):
            await tracker.enter()

    await asyncio.gather(
        dispatch_holds_lock(),
        bridge.prepare_for_place(labware, EXTERNAL_MOVER),
    )

    # The move path runs _do_prepare_for_place -> driver.open(); without the
    # device lock it overlaps the dispatch (max_active == 2).
    assert tracker.max_active == 1
    # Both paths actually ran (rules out a coincidental pass where neither did).
    assert tracker.total_entries == 2


@pytest.mark.asyncio
async def test_real_move_path_serializes_with_dispatch_and_does_not_deadlock() -> None:
    """End-to-end ordering check on the actual move machinery.

    Drives a real ``ExecutableMoveAction`` (transporter.lock -> bridge ->
    device.lock) concurrently with a real ``DeviceCallDispatcher`` dispatch
    on the SAME device. Asserts the device hooks never overlap the dispatch
    (``max_active == 1``) and that the whole thing completes -- the
    ``asyncio.wait_for`` doubles as a deadlock guard, since the lock order is
    transporter.lock then device.lock and a regression that reversed it would
    hang here rather than fail an assertion.
    """
    tracker = _ConcurrencyTracker()
    driver = _InstrDriver("target", tracker)
    target_device = _InstrDevice("target", driver, driver, tracker=tracker)
    target_bridge = LabwareStagingBridge("target", target_device)

    labware = await create_test_labware_instance("plate_1")
    source_pad = PlatePad("source_loc")
    source_pad.initialize_labware(labware)

    source = Location("source_loc", resource=source_pad)
    target = Location("target_loc", resource=target_bridge)
    transporter = create_test_transporter("robot1", ["source_loc", "target_loc"])

    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing = ExecutableMoveAction(
        status_manager=_mock_status_manager(),
        context=_mock_thread_context(),
        action=move,
        labware_location_service=Mock(),
        labware_placer=make_labware_placer(Mock()),
        slot_holder=no_source_hold(),
    )

    dispatcher = DeviceCallDispatcher(
        device=target_device,
        interpreter=None,
        operation_log=[],
        action_id="a1",
        thread_id="t1",
        execution_id="e1",
    )
    request = ActionRequest(
        device_name="target", command="busy_op", args=(), kwargs={},
    )

    # Stub only the sim transporter's physical pick/place (deck-state
    # bookkeeping in the sim driver, unrelated to lock ordering). The
    # transporter.lock acquisition lives in ExecutableMoveAction, and the
    # bridge device-lock hooks (prepare_for_place -> open, notify_placed ->
    # close) stay real -- those are the paths under test.
    with (
        patch.object(transporter, "pick", AsyncMock(return_value=None)),
        patch.object(transporter, "place", AsyncMock(return_value=None)),
    ):
        await asyncio.wait_for(
            asyncio.gather(
                executing.execute(),
                dispatcher.dispatch(request, [], []),
            ),
            timeout=5.0,
        )

    # Device hooks (prepare_for_place -> open, notify_placed -> close) plus
    # the dispatched busy_op all touched the tracker, and none overlapped.
    assert tracker.max_active == 1
    assert tracker.total_entries == 3
    # The move ran to completion: it reached COMPLETED and the plate landed
    # on the target device (notify_placed moves it from stage to loaded).
    assert executing.status == ActionStatus.COMPLETED
    assert labware in target.loaded_labware
