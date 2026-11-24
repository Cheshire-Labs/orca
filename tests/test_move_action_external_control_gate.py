"""Move-action and facade gates for the external-control flag: a hold keeps runs off a device, never the operator."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from typing_extensions import Self
from unittest.mock import Mock

import pytest
from pydantic import JsonValue

from cheshire_drivers import SimShakerDriver

from orca.devices.shaker import Shaker
from orca.gateway.device_command_runner import NamedCommandResult
from orca.gateway.registry import DeviceSnapshot
from orca.operations.operator_device import DeviceBatchExecuteOperation
from orca.operations.operator_device_models import BatchExecuteRequest
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.device_error import DeviceUnderExternalControlError
from orca.resource_models.devices import Device
from orca.resource_models.external_control import device_under_external_control
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.driver_management.device_factory_context import use_device_factory
from orca.driver_management.device_factory_protocol import DeviceDriver
from tests.no_devices import NullDeviceConnectionSource
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
)
from orca.workflow_models.actions.move_action import (
    ExecutableMoveAction,
    MoveAction,
)
from orca.workflow_models.status_enums import ActionStatus
from tests.test_helpers import (
    no_source_hold,
    make_command_pipeline,
    create_test_device,
    create_test_labware_instance,
    create_test_transporter,
)


class TestDeviceUnderExternalControlHelper:

    def test_an_operators_standing_hold_reaches_the_move_gate(self) -> None:
        """A hold and a per-command take must gate identically.

        The gate reads one property; if the hold did not fold into it, a device
        an operator is standing at would still take a plate from an arm.
        """
        device = create_test_device("shaker1")
        device.hold_external_control("swapping the plate by hand")
        loc = Location("loc1", resource=LabwareStagingBridge("bridge1", device))

        assert device_under_external_control(loc) is device

    def test_returns_wrapped_device_when_staging_bridge_holds_flagged_device(
        self,
    ) -> None:
        device = create_test_device("shaker1")
        device.take_external_control()
        bridge = LabwareStagingBridge("bridge1", device)
        loc = Location("loc1", resource=bridge)

        out = device_under_external_control(loc)

        assert out is device

    def test_returns_none_when_staging_bridge_holds_unflagged_device(self) -> None:
        device = create_test_device("shaker1")
        bridge = LabwareStagingBridge("bridge1", device)
        loc = Location("loc1", resource=bridge)

        out = device_under_external_control(loc)

        assert out is None

    def test_returns_none_when_resource_has_no_device(self) -> None:
        pad = PlatePad("pad1")
        loc = Location("loc1", resource=pad)

        out = device_under_external_control(loc)

        assert out is None


def _mock_status_manager() -> Mock:
    sm = Mock()
    sm.set_status = Mock()
    sm.get_status = Mock(return_value="AWAITING_MOVE_RESERVATION")
    return sm


def _mock_thread_context() -> Mock:
    ctx = Mock()
    ctx.execution_id = "exec_1"
    ctx.workflow_name = "wf"
    ctx.thread_id = "thread_1"
    ctx.thread_name = "thread1"
    ctx.template_name = "tmpl_1"
    ctx.group_id = None
    return ctx


async def _build_executing_move(
    *,
    source: Location,
    target: Location,
    transporter: Any,
) -> ExecutableMoveAction:
    labware = await create_test_labware_instance("plate_1")
    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing = ExecutableMoveAction(
        status_manager=_mock_status_manager(),
        context=_mock_thread_context(),
        action=move,
        labware_location_service=Mock(),
        command_pipeline=make_command_pipeline(Mock()),
        slot_holder=no_source_hold(),
    )
    return executing


@pytest.mark.asyncio
async def test_move_refuses_when_source_under_external_control() -> None:
    source_device = create_test_device("source_shaker")
    target_device = create_test_device("target_shaker")
    source_bridge = LabwareStagingBridge("source_bridge", source_device)
    target_bridge = LabwareStagingBridge("target_bridge", target_device)
    source = Location("source_loc", resource=source_bridge)
    target = Location("target_loc", resource=target_bridge)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )
    source_device.take_external_control()

    executing = await _build_executing_move(
        source=source, target=target, transporter=transporter,
    )

    with pytest.raises(DeviceUnderExternalControlError) as excinfo:
        await executing.execute()

    assert excinfo.value.device_name == "source_shaker"
    assert source.labware is None
    assert target.labware is None


@pytest.mark.asyncio
async def test_move_refuses_when_target_under_external_control() -> None:
    source_device = create_test_device("source_shaker")
    target_device = create_test_device("target_shaker")
    source_bridge = LabwareStagingBridge("source_bridge", source_device)
    target_bridge = LabwareStagingBridge("target_bridge", target_device)
    source = Location("source_loc", resource=source_bridge)
    target = Location("target_loc", resource=target_bridge)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )
    target_device.take_external_control()

    executing = await _build_executing_move(
        source=source, target=target, transporter=transporter,
    )

    with pytest.raises(DeviceUnderExternalControlError) as excinfo:
        await executing.execute()

    assert excinfo.value.device_name == "target_shaker"


@pytest.mark.asyncio
async def test_move_refuses_when_transporter_under_external_control() -> None:
    source_pad = PlatePad("source_pad")
    target_pad = PlatePad("target_pad")
    source = Location("source_loc", resource=source_pad)
    target = Location("target_loc", resource=target_pad)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )
    transporter.take_external_control()

    executing = await _build_executing_move(
        source=source, target=target, transporter=transporter,
    )

    with pytest.raises(DeviceUnderExternalControlError) as excinfo:
        await executing.execute()

    assert excinfo.value.device_name == "robot1"


@pytest.mark.asyncio
async def test_move_refuses_when_target_is_staging_bridge_wrapped_device() -> None:
    source_pad = PlatePad("source_pad")
    target_device = create_test_device("target_shaker")
    target_bridge = LabwareStagingBridge("target_bridge", target_device)
    source = Location("source_loc", resource=source_pad)
    target = Location("target_loc", resource=target_bridge)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )
    target_device.take_external_control()

    executing = await _build_executing_move(
        source=source, target=target, transporter=transporter,
    )

    with pytest.raises(DeviceUnderExternalControlError) as excinfo:
        await executing.execute()

    assert excinfo.value.device_name == "target_shaker"


@pytest.mark.asyncio
async def test_move_skips_source_check_when_already_picked() -> None:
    # Retry path: a prior move-attempt left the labware in the gripper.
    # The current move skips pick, so a flag on the source device is
    # not interference and must not block the place.
    source_device = create_test_device("source_shaker")
    target_device = create_test_device("target_shaker")
    source_bridge = LabwareStagingBridge("source_bridge", source_device)
    target_bridge = LabwareStagingBridge("target_bridge", target_device)
    source = Location("source_loc", resource=source_bridge)
    target = Location("target_loc", resource=target_bridge)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )
    source_device.take_external_control()

    labware = await create_test_labware_instance("plate_1")
    # A prior pick left the plate in the jaws; write it where a pick writes it.
    await transporter.gripper_location.place_labware(labware)

    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing = ExecutableMoveAction(
        status_manager=_mock_status_manager(),
        context=_mock_thread_context(),
        action=move,
        labware_location_service=Mock(),
        command_pipeline=make_command_pipeline(Mock()),
        slot_holder=no_source_hold(),
    )

    from unittest.mock import AsyncMock, patch
    with (
        patch.object(transporter, "place", AsyncMock(return_value=None)),
        patch.object(target_bridge, "notify_placed", AsyncMock(return_value=None)),
    ):
        await executing.execute()  # must not raise DeviceUnderExternalControlError


@pytest.mark.asyncio
async def test_move_still_refuses_on_transporter_when_already_picked() -> None:
    # The transporter check must fire on every move, including retries.
    source_pad = PlatePad("source_pad")
    target_pad = PlatePad("target_pad")
    source = Location("source_loc", resource=source_pad)
    target = Location("target_loc", resource=target_pad)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )

    labware = await create_test_labware_instance("plate_1")
    await transporter.gripper_location.place_labware(labware)
    transporter.take_external_control()

    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing = ExecutableMoveAction(
        status_manager=_mock_status_manager(),
        context=_mock_thread_context(),
        action=move,
        labware_location_service=Mock(),
        command_pipeline=make_command_pipeline(Mock()),
        slot_holder=no_source_hold(),
    )

    with pytest.raises(DeviceUnderExternalControlError) as excinfo:
        await executing.execute()

    assert excinfo.value.device_name == "robot1"


@pytest.mark.asyncio
async def test_move_still_refuses_on_target_when_already_picked() -> None:
    # The target check must fire on every move, including retries -- the
    # place will land a plate on the target's device.
    source_pad = PlatePad("source_pad")
    target_device = create_test_device("target_shaker")
    target_bridge = LabwareStagingBridge("target_bridge", target_device)
    source = Location("source_loc", resource=source_pad)
    target = Location("target_loc", resource=target_bridge)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )

    labware = await create_test_labware_instance("plate_1")
    await transporter.gripper_location.place_labware(labware)
    target_device.take_external_control()

    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing = ExecutableMoveAction(
        status_manager=_mock_status_manager(),
        context=_mock_thread_context(),
        action=move,
        labware_location_service=Mock(),
        command_pipeline=make_command_pipeline(Mock()),
        slot_holder=no_source_hold(),
    )

    with pytest.raises(DeviceUnderExternalControlError) as excinfo:
        await executing.execute()

    assert excinfo.value.device_name == "target_shaker"


@pytest.mark.asyncio
async def test_move_gate_raise_records_errored_status() -> None:
    source_device = create_test_device("source_shaker")
    target_device = create_test_device("target_shaker")
    source_bridge = LabwareStagingBridge("source_bridge", source_device)
    target_bridge = LabwareStagingBridge("target_bridge", target_device)
    source = Location("source_loc", resource=source_bridge)
    target = Location("target_loc", resource=target_bridge)
    transporter = create_test_transporter(
        "robot1", ["source_loc", "target_loc"],
    )
    source_device.take_external_control()

    sm = _mock_status_manager()
    labware = await create_test_labware_instance("plate_1")
    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing = ExecutableMoveAction(
        status_manager=sm,
        context=_mock_thread_context(),
        action=move,
        labware_location_service=Mock(),
        command_pipeline=make_command_pipeline(Mock()),
        slot_holder=no_source_hold(),
    )

    with pytest.raises(DeviceUnderExternalControlError):
        await executing.execute()

    statuses_written = [
        call.args[2] for call in sm.set_status.call_args_list
    ]
    assert ActionStatus.ERRORED.name in statuses_written


class _RecordingShaker(SimShakerDriver):

    def __init__(self) -> None:
        super().__init__("recording_shaker")
        self.shake_calls: list[dict[str, Any]] = []
        self.execute_calls: list[tuple[str, dict[str, JsonValue]]] = []

    async def shake(self, request: Any) -> None:  # request: ShakeRequest
        self.shake_calls.append({"speed": request.speed, "duration": request.duration})

    async def execute(self, command: str, options: dict[str, JsonValue]) -> None:
        self.execute_calls.append((command, options))


class _RecordingShakerFactory:
    def __init__(self, driver: _RecordingShaker) -> None:
        self._d = driver

    def build_driver(
        self, device_type: str, name: str,
    ) -> DeviceDriver:
        del device_type, name
        return self._d


def _build_facade_with_device(name: str, device: Device) -> Any:
    from orca.runtime.facades.devices import DeviceFacade
    from tests.gateway_parts import gateway_parts

    system = Mock()
    system.get_device = Mock(return_value=device)
    return DeviceFacade(
        system=system, connections=NullDeviceConnectionSource(), runtime=Mock(),
        device_gateway=gateway_parts().gateway,
    )


class _RecordingRunner:
    """The operator runner, recording what it sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def device(self, device_id: str) -> DeviceSnapshot:
        return DeviceSnapshot(
            type="shaker", name=device_id, interfaces=["IShaker"], capabilities=[], provides_state=False,
            methods={}, site="test", lab="test", workcell=None, status="ready", last_seen=datetime.now(),
        )

    async def run_named(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
    ) -> NamedCommandResult:
        self.sent.append(command)
        return NamedCommandResult(duration_ms=1.0)

    @asynccontextmanager
    async def hold(self, device_id: str) -> AsyncIterator[Self]:
        yield self


@pytest.mark.asyncio
async def test_a_hold_keeps_runs_off_the_device_and_lets_the_operator_through() -> None:
    """The hold is one-way: a run's move to the held device is refused, an operator's command by name is sent."""
    target_device = create_test_device("target_shaker")
    target_device.hold_external_control("driving it by hand")
    source = Location("source_loc", resource=LabwareStagingBridge("source_bridge", create_test_device("source_shaker")))
    target = Location("target_loc", resource=LabwareStagingBridge("target_bridge", target_device))
    move = await _build_executing_move(
        source=source, target=target, transporter=create_test_transporter("robot1", ["source_loc", "target_loc"]),
    )

    with pytest.raises(DeviceUnderExternalControlError):
        await move.execute()

    runtime = Mock()
    runtime.devices = _build_facade_with_device("target_shaker", target_device)
    runtime.operator_base_mode = Mock(return_value=WorkflowRunMode.LIVE)
    runner = _RecordingRunner()
    await DeviceBatchExecuteOperation(runtime=runtime, devices=runner).run(BatchExecuteRequest(
        device_id="target_shaker", commands=[{"command": "shaker.shake"}], stop_on_error=True,
    ))

    assert runner.sent == ["shake"]


def test_a_command_name_drops_only_the_orca_side_namespace() -> None:
    driver = _RecordingShaker()
    with use_device_factory(_RecordingShakerFactory(driver)):
        shaker = Shaker("test_shaker")

    facade = _build_facade_with_device("test_shaker", shaker)

    assert facade.command_name("test_shaker", "shaker.shake") == "shake"
    assert facade.command_name("test_shaker", "shake") == "shake"
