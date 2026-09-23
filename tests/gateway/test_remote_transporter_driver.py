"""Unit tests for `RemoteTransporterDriver`.

The driver is a thin wire-forwarder: every method dispatches via
`DeviceController.execute_command(device_id=name, command=..., params=...,
effective_mode=resolver(name))`. Tests here cover (1) the wire payload
shape per method, (2) effective_mode propagation, (3) typed-response
deserialization for `get_*` methods, (4) `is_initialized` flag flips
on `initialize()` returning.

Mirrors the pattern in `test_remote_device_factory.py::TestRemoteDriverSendIncludesEffectiveMode`
for shaker / centrifuge.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, cast

import pytest

from cheshire_drivers.move_parameters import SEED_MOVE_PARAMETERS
from cheshire_drivers.teachpoints import (
    CartesianCoordinates,
    JointCoordinates,
    Teachpoint,
)
from cheshire_drivers.homing_models import HomeRequest
from cheshire_drivers.labware_models import LabwareIdentity
from cheshire_drivers.transporter_models import (
    CloseGripperRequest,
    EnsureSeededRequest,
    GetCartesianPositionRequest,
    GetJointPositionRequest,
    GetSpeedRequest,
    HaltRequest,
    InitializeRequest,
    MoveSingleAxisRelativeRequest,
    MoveSingleAxisRequest,
    MoveToCoordsRequest,
    MoveToSafeRequest,
    OpenGripperRequest,
    PickAtCoordsRequest,
    PlaceAtCoordsRequest,
    ResetWorldRequest,
    SeedPositionRequest,
    SetFreeModeRequest,
    SetSpeedRequest,
    UnseedPositionRequest,
)

from orca.gateway.controller.command_kind import CommandKind
from orca.runtime.run_modes import WorkflowRunMode

from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_transporter_driver import RemoteTransporterDriver


@dataclass
class _RecordedCall:
    device_id: str
    command: str
    params: Optional[Dict[str, Any]]
    timeout_seconds: Optional[float]
    effective_mode: WorkflowRunMode
    kind: CommandKind


@dataclass
class _FakeController:
    calls: List[_RecordedCall] = field(default_factory=list)
    responses: List[Dict[str, Any]] = field(default_factory=list)

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        effective_mode: WorkflowRunMode = WorkflowRunMode.LIVE,
        resend_on_reconnect: bool = True,
        kind: CommandKind = CommandKind.ACTUATION,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.calls.append(
            _RecordedCall(
                device_id=device_id,
                command=command,
                params=params,
                timeout_seconds=timeout_seconds,
                effective_mode=effective_mode,
                kind=kind,
            )
        )
        if self.responses:
            return self.responses.pop(0)
        return {}


def _make_driver(
    *, mode: WorkflowRunMode = WorkflowRunMode.LIVE,
) -> tuple[RemoteTransporterDriver, _FakeController]:
    controller = _FakeController()
    driver = RemoteTransporterDriver(
        name="arm_1",
        controller=cast(DeviceController, controller),
        mode_resolver=lambda _name: mode,
        timeout=30.0,
    )
    return driver, controller


def _tp(name: str = "pad_1") -> Teachpoint:
    return Teachpoint(
        name,
        CartesianCoordinates(x=100.0, y=0.0, z=50.0, yaw=180.0, pitch=90.0, roll=0.0),
        orientation="right",
        access_type="vertical",
    )


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_flips_is_initialized_flag(self) -> None:
        driver, controller = _make_driver()
        assert driver.is_initialized is False

        await driver.initialize(InitializeRequest())

        assert driver.is_initialized is True
        assert controller.calls[-1].command == "initialize"

    @pytest.mark.asyncio
    async def test_connect_reaches_the_wire(self) -> None:
        """The proxy must forward `connect`, not inherit the interface default.

        `connect`/`disconnect` are concrete on the driver contract so that
        backends with no separate link can honestly do nothing. A remote proxy
        that inherits that default answers success without the command ever
        leaving the process, so the operator sees a connected device and the
        instrument never hears about it.
        """
        driver, controller = _make_driver()

        await driver.connect()

        assert controller.calls[-1].command == "connect"
        assert controller.calls[-1].device_id == "arm_1"

    @pytest.mark.asyncio
    async def test_disconnect_reaches_the_wire_and_clears_initialized(self) -> None:
        driver, controller = _make_driver()
        await driver.initialize(InitializeRequest())

        await driver.disconnect()

        assert controller.calls[-1].command == "disconnect"
        assert driver.is_initialized is False

    @pytest.mark.asyncio
    async def test_home_dispatches_home_command(self) -> None:
        driver, controller = _make_driver()
        await driver.home(HomeRequest())
        assert controller.calls[-1].command == "home"

    @pytest.mark.asyncio
    async def test_move_to_safe_dispatches(self) -> None:
        driver, controller = _make_driver(mode=WorkflowRunMode.LIVE)
        controller.responses.append({"status": "ok"})

        result = await driver.move_to_safe(MoveToSafeRequest())

        assert result is None
        call = controller.calls[-1]
        assert call.command == "move_to_safe"
        assert call.device_id == "arm_1"
        assert call.params == {}
        assert call.timeout_seconds == 30.0
        assert call.effective_mode is WorkflowRunMode.LIVE

    @pytest.mark.asyncio
    async def test_halt_dispatches(self) -> None:
        driver, controller = _make_driver()
        await driver.halt(HaltRequest())
        assert controller.calls[-1].command == "halt"


class TestPickPlaceMoveAtCoords:
    """pick_at_coords / place_at_coords / move_to_coords build the expected
    flat-dict wire payload from the Pydantic Request."""

    @pytest.mark.asyncio
    async def test_pick_at_coords_wire_shape(self) -> None:
        driver, controller = _make_driver()
        request = PickAtCoordsRequest(
            teachpoint=_tp("pad_1"),
            labware_type="Plate_96",
            gateway_path=[_tp("rail_entry"), _tp("hotel_outer")],
            handling=SEED_MOVE_PARAMETERS,
        )

        await driver.pick_at_coords(request)

        call = controller.calls[-1]
        assert call.command == "pick_at_coords"
        assert call.params is not None
        assert call.params["teachpoint"] == _tp("pad_1").to_dict()
        assert call.params["labware_type"] == "Plate_96"
        assert call.params["gateway_path"] == [
            _tp("rail_entry").to_dict(),
            _tp("hotel_outer").to_dict(),
        ]
        assert call.params["handling"] == SEED_MOVE_PARAMETERS.model_dump()

    @pytest.mark.asyncio
    async def test_pick_at_coords_empty_gateway_path(self) -> None:
        driver, controller = _make_driver()
        request = PickAtCoordsRequest(teachpoint=_tp("pad_1"), labware_type="Plate_96", handling=SEED_MOVE_PARAMETERS)
        await driver.pick_at_coords(request)
        call = controller.calls[-1]
        assert call.params is not None
        assert call.params["gateway_path"] == []

    @pytest.mark.asyncio
    async def test_place_at_coords_wire_shape(self) -> None:
        driver, controller = _make_driver()
        request = PlaceAtCoordsRequest(
            teachpoint=_tp("pad_2"),
            labware_type="Plate_96",
            gateway_path=[],
            handling=SEED_MOVE_PARAMETERS,
        )
        await driver.place_at_coords(request)
        call = controller.calls[-1]
        assert call.command == "place_at_coords"
        assert call.params is not None
        assert call.params["teachpoint"] == _tp("pad_2").to_dict()
        assert call.params["labware_type"] == "Plate_96"

    @pytest.mark.asyncio
    async def test_move_to_coords_wire_shape(self) -> None:
        driver, controller = _make_driver()
        await driver.move_to_coords(MoveToCoordsRequest(teachpoint=_tp("waypoint")))
        call = controller.calls[-1]
        assert call.command == "move_to_coords"
        assert call.params == {"teachpoint": _tp("waypoint").to_dict()}
        # Regression guard: move_to_coords must NOT include
        # `labware_type` or `gateway_path` -- MoveToCoordsRequest's
        # _StrictModel base would reject these on orca-client validation.
        # If a future change collapses move_to_coords through the same
        # `_coords_payload` helper as pick/place, this fails loud.
        assert call.params is not None
        assert "labware_type" not in call.params
        assert "gateway_path" not in call.params


class TestSingleAxisAndGripperAndSpeed:
    @pytest.mark.asyncio
    async def test_move_single_axis(self) -> None:
        driver, controller = _make_driver()
        await driver.move_single_axis(MoveSingleAxisRequest(axis="elbow", position=42.0))
        call = controller.calls[-1]
        assert call.command == "move_single_axis"
        assert call.params == {"axis": "elbow", "position": 42.0}

    @pytest.mark.asyncio
    async def test_move_single_axis_relative(self) -> None:
        driver, controller = _make_driver()
        await driver.move_single_axis_relative(
            MoveSingleAxisRelativeRequest(axis="rail", distance=-5.0),
        )
        call = controller.calls[-1]
        assert call.command == "move_single_axis_relative"
        assert call.params == {"axis": "rail", "distance": -5.0}

    @pytest.mark.asyncio
    async def test_open_gripper(self) -> None:
        driver, controller = _make_driver()
        await driver.open_gripper(OpenGripperRequest())
        assert controller.calls[-1].command == "open_gripper"

    @pytest.mark.asyncio
    async def test_close_gripper(self) -> None:
        driver, controller = _make_driver()
        await driver.close_gripper(CloseGripperRequest())
        assert controller.calls[-1].command == "close_gripper"

    @pytest.mark.asyncio
    async def test_set_free_mode(self) -> None:
        driver, controller = _make_driver()
        await driver.set_free_mode(SetFreeModeRequest(axes="all"))
        call = controller.calls[-1]
        assert call.command == "set_free_mode"
        assert call.params == {"axes": "all"}

    @pytest.mark.asyncio
    async def test_set_speed(self) -> None:
        driver, controller = _make_driver()
        await driver.set_speed(SetSpeedRequest(speed=0.75))
        call = controller.calls[-1]
        assert call.command == "set_speed"
        assert call.params == {"speed": 0.75}

    @pytest.mark.asyncio
    async def test_get_speed_decodes_response(self) -> None:
        driver, controller = _make_driver()
        controller.responses.append({"speed": 0.42})
        result = await driver.get_speed(GetSpeedRequest())
        assert result == 0.42
        assert controller.calls[-1].command == "get_speed"

    @pytest.mark.asyncio
    async def test_get_speed_raises_on_malformed_response(self) -> None:
        """SpeedResponse is `_StrictModel(extra="forbid")` and requires a
        `speed: float`, so malformed wire responses raise Pydantic
        ValidationError -- consistent with how get_joint_position /
        get_cartesian_position validate their typed responses."""
        from pydantic import ValidationError

        driver, controller = _make_driver()
        controller.responses.append({"unexpected": "shape"})
        with pytest.raises(ValidationError):
            await driver.get_speed(GetSpeedRequest())


class TestPositionQueries:
    @pytest.mark.asyncio
    async def test_get_joint_position_decodes_response(self) -> None:
        driver, controller = _make_driver()
        controller.responses.append(
            {"rail": 1.0, "base": 2.0, "shoulder": 3.0, "elbow": 4.0, "wrist": 5.0, "gripper": 6.0},
        )
        result = await driver.get_joint_position(GetJointPositionRequest())
        assert isinstance(result, JointCoordinates)
        assert result.rail == 1.0
        assert result.elbow == 4.0
        assert controller.calls[-1].command == "get_joint_position"

    @pytest.mark.asyncio
    async def test_get_cartesian_position_decodes_response(self) -> None:
        driver, controller = _make_driver()
        controller.responses.append(
            {"x": 10.0, "y": 20.0, "z": 30.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0},
        )
        result = await driver.get_cartesian_position(GetCartesianPositionRequest())
        assert isinstance(result, CartesianCoordinates)
        assert result.x == 10.0
        assert result.roll == 180.0
        assert controller.calls[-1].command == "get_cartesian_position"


class TestEffectiveModePropagation:
    @pytest.mark.asyncio
    async def test_dispatch_carries_live_mode(self) -> None:
        driver, controller = _make_driver(mode=WorkflowRunMode.LIVE)
        await driver.pick_at_coords(
            PickAtCoordsRequest(teachpoint=_tp("pad_1"), labware_type="Plate_96", handling=SEED_MOVE_PARAMETERS),
        )
        assert controller.calls[-1].effective_mode is WorkflowRunMode.LIVE

    @pytest.mark.asyncio
    async def test_dispatch_carries_device_sim_mode(self) -> None:
        driver, controller = _make_driver(mode=WorkflowRunMode.DEVICE_SIM)
        await driver.pick_at_coords(
            PickAtCoordsRequest(teachpoint=_tp("pad_1"), labware_type="Plate_96", handling=SEED_MOVE_PARAMETERS),
        )
        assert controller.calls[-1].effective_mode is WorkflowRunMode.DEVICE_SIM


class TestInterfacesContract:
    def test_advertises_what_an_arm_is_and_that_it_homes(self) -> None:
        """An arm's axes lose their reference at power-off and bring-up no longer
        homes, so homing is part of what a transporter offers, not an extra."""
        assert RemoteTransporterDriver.interfaces == frozenset(
            {"ITransporter", "IHomeable"},
        )

    def test_name_property_returns_constructed_name(self) -> None:
        driver, _ = _make_driver()
        assert driver.name == "arm_1"


class TestTimeoutPlumbing:
    """`timeout` constructor arg flows through to every wire dispatch.

    Regression guard: if `_send` ever stops passing `timeout_seconds=self._timeout`
    to the controller, this fires. The factory builds drivers with
    `timeout=factory._default_timeout`, so a broken plumbing would
    silently default-zero on the wire instead of honoring the
    deployment's configured per-command timeout.
    """

    @pytest.mark.asyncio
    async def test_timeout_passed_to_controller_on_pick_at_coords(self) -> None:
        controller = _FakeController()
        driver = RemoteTransporterDriver(
            name="arm_1",
            controller=cast(DeviceController, controller),
            mode_resolver=lambda _name: WorkflowRunMode.LIVE,
            timeout=42.5,
        )
        await driver.pick_at_coords(
            PickAtCoordsRequest(teachpoint=_tp("pad_1"), labware_type="Plate_96", handling=SEED_MOVE_PARAMETERS),
        )
        assert controller.calls[-1].timeout_seconds == 42.5

    @pytest.mark.asyncio
    async def test_default_timeout_is_30_seconds(self) -> None:
        controller = _FakeController()
        driver = RemoteTransporterDriver(
            name="arm_1",
            controller=cast(DeviceController, controller),
            mode_resolver=lambda _name: WorkflowRunMode.LIVE,
        )
        await driver.home(HomeRequest())
        assert controller.calls[-1].timeout_seconds == 30.0


class TestTheDriverSaysWhichCommandsTouchNothing:
    """The four world ops push the driver's picture of the deck and move no
    hardware. Declaring that is what keeps them out of the device lock, off
    the faulted-device refusal, and unable to fault an arm they never moved.
    """

    def _identity(self) -> LabwareIdentity:
        return LabwareIdentity(
            labware_id="lw_1", barcode=None, labware_type="Plate_96",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        "seed_position", "ensure_seeded", "unseed_position", "reset_world",
    ])
    async def test_a_world_op_declares_itself(self, command: str) -> None:
        driver, controller = _make_driver()
        requests = {
            "seed_position": SeedPositionRequest(
                position_id="pad_1", labware=self._identity(),
            ),
            "ensure_seeded": EnsureSeededRequest(
                position_id="pad_1", labware=self._identity(),
            ),
            "unseed_position": UnseedPositionRequest(
                position_id="pad_1", labware=self._identity(),
            ),
            "reset_world": ResetWorldRequest(),
        }

        await getattr(driver, command)(requests[command])

        call = controller.calls[-1]
        assert call.command == command
        assert call.kind is CommandKind.WORLD_SYNC

    @pytest.mark.asyncio
    async def test_a_move_does_not(self) -> None:
        """Control. Without it the assertion above would pass on a driver that
        declared WORLD_SYNC for everything it sends."""
        driver, controller = _make_driver()

        await driver.pick_at_coords(PickAtCoordsRequest(
            teachpoint=_tp("pad_1"), labware_type="Plate_96",
            handling=SEED_MOVE_PARAMETERS,
        ))

        assert controller.calls[-1].kind is CommandKind.ACTUATION
