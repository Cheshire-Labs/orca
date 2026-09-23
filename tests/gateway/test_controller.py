"""Tests for device controller."""

import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime

from orca.gateway.controller.command_kind import CommandKind
from orca.gateway.controller import (
    DeviceController,
    DeviceLockedError,
    DeviceOfflineError,
    CommandTimeoutError,
    InvalidCommandError,
)
from orca.gateway.controller.controller import _PendingCommand
from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    instrument_outcome_of,
)
from cheshire_drivers.driver_errors import InstrumentOutcome
from orca.runtime.run_modes import WorkflowRunMode
from cheshire_drivers.gateway_protocol import ResponseMessage
from orca.gateway.registry.snapshot import DeviceSnapshot
from tests.test_helpers import wait_until


def _busy_pending(
    device_id: str,
    command_id: str,
    *,
    command: str = "shake",
) -> _PendingCommand:
    """Build a _PendingCommand to mark a device busy in _pending.

    Mirrors how the controller records an in-flight command since
    _pending is the single source of truth for "device is busy".
    """
    return _PendingCommand(
        command_id=command_id,
        device_id=device_id,
        command=command,
        params={"speed": 500.0, "duration": 1.0},
        effective_mode=WorkflowRunMode.LIVE,
        timeout_seconds=30.0,
        future=asyncio.Future(),
    )


def _snapshot(
    device_id: str,
    *,
    type: str = "shaker",
    interfaces: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> DeviceSnapshot:
    """Build a DeviceSnapshot for tests with sensible defaults."""
    return DeviceSnapshot(
        type=type,
        name=device_id,
        interfaces=interfaces or [],
        capabilities=capabilities or [],
        provides_state=False,
        methods={},
        site="test-site",
        lab="test-lab",
        workcell=None,
        status="ready",
        last_seen=datetime.utcnow(),
    )


@pytest.fixture
def controller():
    """Create a fresh controller for each test."""
    return DeviceController()


@pytest.mark.asyncio
class TestDeviceController:
    """Tests for DeviceController class."""

    async def test_is_locked_returns_false_for_unlocked_device(self, controller):
        """Test is_locked returns False for device with no active command."""
        is_locked = await controller.is_locked("device_1")
        assert is_locked is False

    async def test_is_locked_returns_true_for_locked_device(self, controller):
        """Test is_locked returns True for device with active command."""
        # Manually lock device
        async with controller._lock:
            controller._pending["device_1"] = _busy_pending("device_1", "cmd_123")

        is_locked = await controller.is_locked("device_1")
        assert is_locked is True

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_get_available_devices_filters_locked(self, mock_registry, controller):
        """Test get_available_devices filters out locked devices."""
        # Mock registry returns 3 devices
        mock_registry.list_devices = AsyncMock(
            return_value=[
                _snapshot("device_1", interfaces=["IShaker"]),
                _snapshot("device_2", interfaces=["IShaker"]),
                _snapshot("device_3", interfaces=["IShaker"]),
            ]
        )

        # Lock device_2
        async with controller._lock:
            controller._pending["device_2"] = _busy_pending("device_2", "cmd_456")

        # Get available devices
        available = await controller.get_available_devices(device_type="shaker")

        # Should only return device_1 and device_3
        assert len(available) == 2
        names = [d.name for d in available]
        assert "device_1" in names
        assert "device_3" in names
        assert "device_2" not in names

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_fails_if_device_offline(
        self, mock_registry, controller
    ):
        """Test execute_command raises DeviceOfflineError if device not found."""
        mock_registry.get_device = AsyncMock(return_value=None)

        with pytest.raises(DeviceOfflineError, match="not found or offline"):
            await controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, effective_mode=WorkflowRunMode.LIVE)

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_fails_if_invalid_command(
        self, mock_registry, controller
    ):
        """Test execute_command raises InvalidCommandError for unsupported command."""
        # Device exists
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )

        # Try to send "centrifuge" command to shaker
        with pytest.raises(InvalidCommandError, match="does not support"):
            await controller.execute_command("device_1", "centrifuge", {"g": 1000}, effective_mode=WorkflowRunMode.LIVE)

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_lh_preflight_rejects_unknown_field(
        self, mock_registry, mock_conn_manager, controller
    ):
        """LH pre-flight rejects unknown fields BEFORE WebSocket dispatch.

        The whole point of the controller's pre-flight validate_lh_payload call
        is to fail at a hosted deployment's network boundary instead of after a round-trip
        TypeError on orca-client. If this regresses, callers see the failure
        only after the command travels the wire and the executor on the other
        side blows up.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "lh_1",
                type="liquid_handler",
                interfaces=["ILiquidHandler", "IProtocolRunner"],
            ),
        )
        mock_conn_manager.send_to_client = AsyncMock()

        bad_aspirate_params = {
            "labware": "plate1",
            "positions": ["A1"],
            "volumes": [50.0],
            "totally_unknown_field": "this is not a real field",
        }

        with pytest.raises(InvalidCommandError, match="Invalid params for 'aspirate'"):
            await controller.execute_command("lh_1", "aspirate", bad_aspirate_params, effective_mode=WorkflowRunMode.LIVE)

        mock_conn_manager.send_to_client.assert_not_called()

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_lh_preflight_rejects_bad_shape(
        self, mock_registry, mock_conn_manager, controller
    ):
        """LH pre-flight catches Pydantic ValidationError on malformed payloads.

        Same boundary contract as the unknown-field test, exercised through a
        type-mismatch path so both ValueError and ValidationError mappings to
        InvalidCommandError are covered.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "lh_1",
                type="liquid_handler",
                interfaces=["ILiquidHandler", "IProtocolRunner"],
            ),
        )
        mock_conn_manager.send_to_client = AsyncMock()

        # `positions` must be a list[str]; passing an int forces Pydantic to fail.
        bad_aspirate_params = {
            "labware": "plate1",
            "positions": 42,
            "volumes": [50.0],
        }

        with pytest.raises(InvalidCommandError, match="Invalid params for 'aspirate'"):
            await controller.execute_command("lh_1", "aspirate", bad_aspirate_params, effective_mode=WorkflowRunMode.LIVE)

        mock_conn_manager.send_to_client.assert_not_called()

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_liquid_probe_preflight_rejects_a_shapeless_request(
        self, mock_registry, mock_conn_manager, controller
    ):
        """A container probe names no wells, so it has to name the channels instead
        or there is no channel count. Caught here, before the wire."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "lh_1",
                type="liquid_handler",
                interfaces=["ILiquidHandler", "ILiquidProbe"],
            ),
        )
        mock_conn_manager.send_to_client = AsyncMock()

        with pytest.raises(InvalidCommandError, match="Invalid params for 'liquid_probe'"):
            await controller.execute_command(
                "lh_1", "liquid_probe", {"labware": "trough_1"},
                effective_mode=WorkflowRunMode.LIVE,
            )

        mock_conn_manager.send_to_client.assert_not_called()

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_liquid_probe_reaches_a_head_that_can_sense(
        self, mock_registry, mock_conn_manager, controller
    ):
        """The refusals above are only half the gate: a head that does advertise
        the sensor has to get the command through, or probing works nowhere."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "lh_1",
                type="liquid_handler",
                interfaces=["ILiquidHandler", "ILiquidProbe"],
            ),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        task = asyncio.create_task(
            controller.execute_command(
                "lh_1", "liquid_probe", {"labware": "plate_1", "positions": ["B3"]},
                timeout_seconds=1, effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(lambda: mock_conn_manager.send_to_client.called)

        message_json = mock_conn_manager.send_to_client.call_args[0][1]
        assert "liquid_probe" in message_json
        assert "plate_1" in message_json

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_transporter_preflight_rejects_unknown_field(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Transporter pre-flight rejects unknown fields BEFORE WebSocket dispatch.

        Regression guard mirroring the LH pre-flight test. If this
        regresses, callers see the failure only after the command travels the
        wire and the executor on the other side blows up.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "arm_1", type="transporter", interfaces=["ITransporter"],
            ),
        )
        mock_conn_manager.send_to_client = AsyncMock()

        bad_pick_params = {
            "teachpoint": {
                "name": "nest_1",
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "yaw": 0.0,
                "pitch": 0.0,
                "roll": 0.0,
                "orientation": "left",
                "access_type": "vertical",
                "gripper_offset": 20.0,
                "vertical_clearance": 20.0,
                "horizontal_clearance": 100.0,
            },
            "labware_type": "Plate_96",
            "totally_unknown_field": "this is not a real field",
        }

        with pytest.raises(
            InvalidCommandError, match="Invalid params for 'pick_at_coords'",
        ):
            await controller.execute_command(
                "arm_1", "pick_at_coords", bad_pick_params,
                effective_mode=WorkflowRunMode.LIVE,
            )

        mock_conn_manager.send_to_client.assert_not_called()

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_fails_if_device_locked(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Test execute_command raises DeviceLockedError if device is busy."""
        # Device exists
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )

        # Lock device
        async with controller._lock:
            controller._pending["device_1"] = _busy_pending("device_1", "cmd_999")

        # Try to execute command
        with pytest.raises(DeviceLockedError, match="is busy"):
            await controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, effective_mode=WorkflowRunMode.LIVE)

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_sends_to_client(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Test execute_command sends CommandMessage to client."""
        # Device exists and online
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Execute command in background (will timeout, but we just want to verify send)
        task = asyncio.create_task(
            controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, timeout_seconds=1, effective_mode=WorkflowRunMode.LIVE)
        )

        # Wait for the command to reach the wire.
        await wait_until(lambda: mock_conn_manager.send_to_client.called)

        # Verify send_to_client was called
        assert mock_conn_manager.send_to_client.called
        call_args = mock_conn_manager.send_to_client.call_args
        client_id = call_args[0][0]
        message_json = call_args[0][1]

        assert client_id == "client_1"
        assert "shake" in message_json
        assert "device_1" in message_json

        # Cancel task to avoid timeout
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_waits_for_response(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Test execute_command waits for and returns response."""
        # Device exists and online
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Execute command in background
        task = asyncio.create_task(
            controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, timeout_seconds=5, effective_mode=WorkflowRunMode.LIVE)
        )

        # Wait for the pending record so we can read its command_id.
        await wait_until(lambda: "device_1" in controller._pending)
        async with controller._lock:
            command_id = controller._pending["device_1"].command_id

        assert command_id is not None

        # Simulate response from client
        response = ResponseMessage(
            command_id=command_id,
            success=True,
            result={"status": "completed"},
            error=None,
            error_type=None,
        )
        await controller.handle_response(response)

        # Wait for execute_command to complete
        result = await task

        # Verify result
        assert result == {"status": "completed"}

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_times_out(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Test execute_command raises CommandTimeoutError if no response."""
        # Device exists and online
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Execute command with short timeout
        with pytest.raises(CommandTimeoutError, match="timed out"):
            await controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, timeout_seconds=0.5, effective_mode=WorkflowRunMode.LIVE)

        # Verify device is unlocked after timeout
        is_locked = await controller.is_locked("device_1")
        assert is_locked is False

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_unlocks_on_exception(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Test device is unlocked even if exception occurs."""
        # Device exists but send fails
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=False)  # Send fails

        with pytest.raises(DeviceOfflineError, match="Failed to send"):
            await controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, effective_mode=WorkflowRunMode.LIVE)

        # Verify device is unlocked
        is_locked = await controller.is_locked("device_1")
        assert is_locked is False

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_execute_command_includes_effective_mode_on_wire(
        self, mock_registry, mock_conn_manager, controller
    ):
        """The CommandMessage payload sent over the wire carries effective_mode."""
        from orca.runtime.run_modes import WorkflowRunMode

        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        task = asyncio.create_task(
            controller.execute_command(
                "device_1",
                "shake",
                {"speed": 500.0, "duration": 1.0},
                timeout_seconds=1,
                effective_mode=WorkflowRunMode.DEVICE_SIM,
            )
        )
        await wait_until(lambda: mock_conn_manager.send_to_client.called)

        assert mock_conn_manager.send_to_client.called
        message_json = mock_conn_manager.send_to_client.call_args[0][1]
        # Wire payload is the model_dump_json of MessageEnvelope. The
        # effective_mode of the inner CommandMessage must be present.
        assert "DEVICE_SIM" in message_json

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_execute_command_pure_sim_rejected_before_send(self, controller):
        """PURE_SIM must never reach the gateway; raise InvalidCommandError early."""
        from orca.runtime.run_modes import WorkflowRunMode

        with pytest.raises(InvalidCommandError, match="PURE_SIM"):
            await controller.execute_command(
                "device_1",
                "shake",
                {"speed": 500.0, "duration": 1.0},
                effective_mode=WorkflowRunMode.PURE_SIM,
            )

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_effective_interfaces_blocks_capability_drift(
        self, mock_registry, controller,
    ):
        """effective_interfaces narrows beyond what the connection tracker advertises.

        Connection-tracker advertises [IShaker, IReader] (driver class
        capability is broader). Topology declared only [IShaker]. The
        gateway's effective_interfaces resolution intersects to [IShaker],
        so an IReader method (`read`) must be rejected even though the
        connection-tracker advertised IReader.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "device_1", interfaces=["IShaker", "IReader"], capabilities=[],
            ),
        )

        with pytest.raises(InvalidCommandError, match="does not support"):
            await controller.execute_command(
                "device_1",
                "read",
                {},
                effective_interfaces=frozenset({"IShaker"}),
                effective_mode=WorkflowRunMode.LIVE,
            )

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_effective_interfaces_allows_declared_methods(
        self, mock_registry, controller,
    ):
        """Methods on declared interfaces still pass with effective_interfaces.

        A `shake` method (on IShaker) on a [IShaker, IReader]-advertised
        device passes when topology declared IShaker. Validation runs to
        completion (we mock the wire send to not actually go out).
        """
        from orca.gateway.controller.controller import connection_manager

        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "device_1", interfaces=["IShaker", "IReader"], capabilities=[],
            ),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        with patch.object(connection_manager, "send_to_client", AsyncMock(return_value=True)):
            task = asyncio.create_task(
                controller.execute_command(
                    "device_1",
                    "shake",
                    {"speed": 500.0, "duration": 1.0},
                    timeout_seconds=0.05,
                    effective_interfaces=frozenset({"IShaker"}),
                    effective_mode=WorkflowRunMode.LIVE,
                )
            )
            try:
                # Wait for the task to time out (no response future completed).
                # The capability check must have passed for the task to reach
                # the wait-for-response stage.
                with pytest.raises(CommandTimeoutError):
                    await task
            except asyncio.CancelledError:
                pass

    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_effective_interfaces_none_falls_back_to_advertised(
        self, mock_registry, controller,
    ):
        """effective_interfaces=None falls back to the advertised set.

        When the gateway can't resolve the registry entry (runtime not
        ready, no entry, etc.), it omits the kwarg. The controller falls
        back to the connection tracker's advertised set so paths without
        runtime context still work.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot(
                "device_1", interfaces=["IShaker"], capabilities=[],
            ),
        )

        # `read` is on IReader, not in advertised; rejected.
        with pytest.raises(InvalidCommandError, match="does not support"):
            await controller.execute_command("device_1", "read", {}, effective_mode=WorkflowRunMode.LIVE)

    async def test_handle_response_completes_future(self, controller):
        """Test handle_response completes the waiting future."""
        # Create a future
        future = asyncio.Future()
        async with controller._lock:
            controller._command_futures["cmd_123"] = future

        # Handle response
        response = ResponseMessage(
            command_id="cmd_123",
            success=True,
            result={"status": "done"},
            error=None,
            error_type=None,
        )
        await controller.handle_response(response)

        # Verify future is completed
        assert future.done()
        assert await future == {"status": "done"}

    async def test_handle_response_sets_exception_on_error(self, controller):
        """Test handle_response sets exception if command failed."""
        # Create a future
        future = asyncio.Future()
        async with controller._lock:
            controller._command_futures["cmd_456"] = future

        # Handle error response
        response = ResponseMessage(
            command_id="cmd_456",
            success=False,
            result=None,
            error="Device error: motor stalled",
            error_type="MotorStallError",
        )
        await controller.handle_response(response)

        # Verify future has exception
        assert future.done()
        with pytest.raises(Exception, match="motor stalled"):
            await future

    @pytest.mark.parametrize("declared", list(InstrumentOutcome))
    async def test_handle_response_carries_the_outcome_off_the_wire(
        self, controller, declared: InstrumentOutcome,
    ):
        """The one place the wire's answer becomes an exception field.

        Everything downstream reads that field and nothing else: whether to
        fault the device, what state to record it in, and whether waiting is
        worth anything. Drop it here and every remote failure reads FAILED, so
        a busy handler's refusal both fails the move and faults the handler.
        """
        future = asyncio.Future()
        async with controller._lock:
            controller._command_futures["cmd_out"] = future

        await controller.handle_response(ResponseMessage(
            command_id="cmd_out", success=False, result=None,
            error="still holds tips", error_type="GantryBusyError",
            instrument_outcome=declared,
        ))

        with pytest.raises(CommandExecutionError) as raised:
            await future
        assert instrument_outcome_of(raised.value) is declared

    async def test_an_agent_that_says_nothing_reads_as_failed(self, controller):
        """No field on the wire is the contract's way of saying the driver did
        not classify, and FAILED is the reading that costs least when wrong."""
        future = asyncio.Future()
        async with controller._lock:
            controller._command_futures["cmd_quiet"] = future

        await controller.handle_response(ResponseMessage(
            command_id="cmd_quiet", success=False, result=None,
            error="motor stalled", error_type="MotorStallError",
        ))

        with pytest.raises(CommandExecutionError) as raised:
            await future
        assert instrument_outcome_of(raised.value) is InstrumentOutcome.FAILED

    async def test_handle_response_ignores_already_settled_future(self, controller):
        """A late wire response for a command whose future the timeout watcher
        already failed must be dropped, not re-set. The timeout watcher fails
        the future but the ``finally`` pop runs a turn later, so a response
        landing in that window finds a done future still in ``_command_futures``.
        Re-setting it raises InvalidStateError, which would propagate up the WS
        receive loop and disconnect the whole client."""
        future: asyncio.Future = asyncio.Future()
        future.set_exception(CommandTimeoutError("already timed out"))
        async with controller._lock:
            controller._command_futures["cmd_race"] = future

        response = ResponseMessage(
            command_id="cmd_race",
            success=True,
            result={"status": "done"},
            error=None,
            error_type=None,
        )
        # Must not raise InvalidStateError.
        await controller.handle_response(response)

        with pytest.raises(CommandTimeoutError, match="already timed out"):
            await future

    async def test_emergency_unlock_removes_lock(self, controller):
        """Test emergency_unlock forcibly releases device lock."""
        # Lock device
        pending = _busy_pending("device_1", "cmd_789")
        async with controller._lock:
            controller._pending["device_1"] = pending
            controller._command_futures["cmd_789"] = pending.future

        # Verify locked
        is_locked = await controller.is_locked("device_1")
        assert is_locked is True

        # Emergency unlock
        await controller.emergency_unlock("device_1")

        # Verify unlocked
        is_locked = await controller.is_locked("device_1")
        assert is_locked is False

    async def test_emergency_unlock_cancels_future(self, controller):
        """Test emergency_unlock cancels the waiting future."""
        # Lock device with future
        pending = _busy_pending("device_1", "cmd_999")
        future = pending.future
        async with controller._lock:
            controller._pending["device_1"] = pending
            controller._command_futures["cmd_999"] = future

        # Emergency unlock
        await controller.emergency_unlock("device_1")

        # Verify future is cancelled
        assert future.cancelled()

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_concurrent_commands_to_same_device_fail(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Test concurrent commands to same device - second should fail."""
        # Device exists and online
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Start first command (will timeout, but that's ok)
        task1 = asyncio.create_task(
            controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, timeout_seconds=2, effective_mode=WorkflowRunMode.LIVE)
        )

        # Wait for the first command to acquire the lock (its pending record).
        await wait_until(lambda: "device_1" in controller._pending)

        # Try second command - should fail immediately
        with pytest.raises(DeviceLockedError, match="is busy"):
            await controller.execute_command("device_1", "shake", {"speed": 300, "duration": 1.0}, effective_mode=WorkflowRunMode.LIVE)

        # Cancel first task
        task1.cancel()
        try:
            await task1
        except (asyncio.CancelledError, CommandTimeoutError):
            pass

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_concurrent_commands_to_different_devices_succeed(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Test concurrent commands to different devices both succeed."""
        # Both devices exist
        def mock_get_device(device_id):
            return _snapshot(device_id, interfaces=["IShaker"])

        mock_registry.get_device = AsyncMock(side_effect=mock_get_device)
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Start both commands
        task1 = asyncio.create_task(
            controller.execute_command("device_1", "shake", {"speed": 500, "duration": 1.0}, timeout_seconds=2, effective_mode=WorkflowRunMode.LIVE)
        )
        task2 = asyncio.create_task(
            controller.execute_command("device_2", "shake", {"speed": 300, "duration": 1.0}, timeout_seconds=2, effective_mode=WorkflowRunMode.LIVE)
        )

        # Wait for both commands to acquire their locks (pending records).
        await wait_until(
            lambda: "device_1" in controller._pending
            and "device_2" in controller._pending
        )

        # Both should have acquired locks
        async with controller._lock:
            assert "device_1" in controller._pending
            assert "device_2" in controller._pending
            cmd1 = controller._pending["device_1"].command_id
            cmd2 = controller._pending["device_2"].command_id

        # Simulate responses
        response1 = ResponseMessage(
            command_id=cmd1, success=True, result={"status": "done"}, error=None, error_type=None
        )
        response2 = ResponseMessage(
            command_id=cmd2, success=True, result={"status": "done"}, error=None, error_type=None
        )

        await controller.handle_response(response1)
        await controller.handle_response(response2)

        # Both should complete
        result1 = await task1
        result2 = await task2

        assert result1 == {"status": "done"}
        assert result2 == {"status": "done"}

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_reserve_creates_pending_record_before_dispatch(
        self, mock_registry, mock_conn_manager, controller
    ):
        """A3 fix: the ``_pending`` record exists from reservation, before the wire send.

        The record must own the device the instant it's reserved so a
        disconnect caught while the dispatch is in flight finds it. We
        capture ``_pending`` membership from inside ``send_to_client`` --
        the dispatch -- to prove the record was already there when the
        command went out.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")

        seen = {}

        async def capture_then_succeed(client_id, message_json):
            seen["pending_at_dispatch"] = "device_1" in controller._pending
            seen["command_id_at_dispatch"] = (
                controller._pending["device_1"].command_id
                if "device_1" in controller._pending
                else None
            )
            return True

        mock_conn_manager.send_to_client = AsyncMock(
            side_effect=capture_then_succeed,
        )

        task = asyncio.create_task(
            controller.execute_command(
                "device_1", "shake", {"speed": 500, "duration": 1.0},
                timeout_seconds=5,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(lambda: "pending_at_dispatch" in seen)

        assert seen["pending_at_dispatch"] is True
        assert seen["command_id_at_dispatch"] is not None

        # Resolve so the task doesn't leak.
        await controller.handle_response(ResponseMessage(
            command_id=seen["command_id_at_dispatch"], success=True,
            result={"ok": True}, error=None, error_type=None,
        ))
        await task

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_emergency_unlock_clears_pending_and_cancels_timers(
        self, mock_registry, mock_conn_manager, controller
    ):
        """A14 fix: emergency_unlock removes the ``_pending`` record and cancels its timers.

        Before the fix, emergency_unlock popped only the busy table and
        left the ``_pending`` entry plus its armed command_timer running
        -- a leak. The unlock must clear the record entirely and cancel
        any live timer.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("device_1", interfaces=["IShaker"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        task = asyncio.create_task(
            controller.execute_command(
                "device_1", "shake", {"speed": 500, "duration": 1.0},
                timeout_seconds=30,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(
            lambda: "device_1" in controller._pending
            and controller._pending["device_1"].command_timer is not None
        )

        async with controller._lock:
            assert "device_1" in controller._pending
            pending = controller._pending["device_1"]
        command_timer = pending.command_timer
        assert command_timer is not None

        await controller.emergency_unlock("device_1")

        # The pending record is gone.
        async with controller._lock:
            assert "device_1" not in controller._pending
        # Its command_timer was cancelled (no orphan task left running).
        await asyncio.sleep(0)
        assert command_timer.cancelled() or command_timer.done()

        # The held future was cancelled by the unlock; drain the task.
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except (asyncio.CancelledError, CommandTimeoutError):
            pass


_WORLD_SYNC_PARAMS = {
    "position_id": "pos_1",
    "labware": {"labware_id": "lw_1", "labware_type": "plate_96"},
}


@pytest.mark.asyncio
class TestWorldSyncBypass:
    """The ``CommandKind.WORLD_SYNC`` bypass on execute_command.

    ``ensure_seeded``, ``seed_position``, ``unseed_position`` and
    ``reset_world`` update orca-client's sim graph rather than acting on the
    device, so they must not contend with workflow commands at the
    controller's per-device exclusivity lock.
    """

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_bypasses_busy_check(
        self, mock_registry, mock_conn_manager, controller
    ):
        """World-sync op does NOT raise DeviceLockedError when device is busy."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Pretend a workflow command is already busy on this device.
        async with controller._lock:
            controller._pending["xporter_1"] = _busy_pending(
                "xporter_1", "workflow_cmd_123", command="home",
            )

        # World-sync op must NOT raise DeviceLockedError. Start it; it
        # will block on its future. The fact it gets past the lock-acquire
        # block and into the wire-send proves the bypass.
        task = asyncio.create_task(
            controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=5, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        # Wait for the world-sync command to reach the wire (its future registered).
        await wait_until(
            lambda: any(
                cid != "workflow_cmd_123" for cid in controller._command_futures
            )
        )
        assert not task.done() or task.exception() is None
        # Resolve via fake response.
        async with controller._lock:
            command_id = next(
                cid for cid in controller._command_futures
                if cid != "workflow_cmd_123"
            )
        await controller.handle_response(ResponseMessage(
            command_id=command_id, success=True, result={"ok": True},
            error=None, error_type=None,
        ))
        result = await task
        assert result == {"ok": True}

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_does_not_claim_device_busy(
        self, mock_registry, mock_conn_manager, controller
    ):
        """World-sync op never claims the device-busy table -- before, during, or after.

        ``_pending`` is the single source of truth for "device is busy /
        which command owns it". A world-sync op must never register there,
        or it would lock out concurrent workflow commands on the device.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        task = asyncio.create_task(
            controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=5, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(lambda: len(controller._command_futures) > 0)
        # In flight: the busy table should NOT contain xporter_1.
        async with controller._lock:
            assert "xporter_1" not in controller._pending
            command_id = next(iter(controller._command_futures))
        await controller.handle_response(ResponseMessage(
            command_id=command_id, success=True, result={}, error=None, error_type=None,
        ))
        await task
        # After completion: still not present.
        async with controller._lock:
            assert "xporter_1" not in controller._pending

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_does_not_register_in_pending(
        self, mock_registry, mock_conn_manager, controller
    ):
        """World-sync op never touches ``_pending`` -- preserves any workflow command's entry."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        task = asyncio.create_task(
            controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=5, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(lambda: len(controller._command_futures) > 0)
        async with controller._lock:
            assert "xporter_1" not in controller._pending
            command_id = next(iter(controller._command_futures))
        await controller.handle_response(ResponseMessage(
            command_id=command_id, success=True, result={}, error=None, error_type=None,
        ))
        await task
        async with controller._lock:
            assert "xporter_1" not in controller._pending

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_does_not_clobber_workflow_command_pending(
        self, mock_registry, mock_conn_manager, controller
    ):
        """A world-sync op's finally must NOT pop the workflow command's ``_pending`` entry.

        Regression for the bug shape: ``_pending`` is keyed
        one-per-device; an unguarded pop on world-sync's finally would
        clobber the in-flight workflow command's pending entry and
        break disconnect-recovery for it.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Start a workflow command (it will sit waiting for response).
        wf_task = asyncio.create_task(
            controller.execute_command(
                "xporter_1", "home", {},
                timeout_seconds=10,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(lambda: "xporter_1" in controller._pending)
        async with controller._lock:
            assert "xporter_1" in controller._pending
            wf_command_id = controller._pending["xporter_1"].command_id
            wf_pending_obj_id = id(controller._pending["xporter_1"])

        # Now run a world-sync op to completion on the same device.
        ss_task = asyncio.create_task(
            controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=5, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(
            lambda: any(
                cid != wf_command_id for cid in controller._command_futures
            )
        )
        async with controller._lock:
            ss_command_id = next(
                cid for cid in controller._command_futures
                if cid != wf_command_id
            )
        await controller.handle_response(ResponseMessage(
            command_id=ss_command_id, success=True, result={}, error=None, error_type=None,
        ))
        await ss_task

        # The workflow command's ``_pending`` entry must still be the same object.
        async with controller._lock:
            assert "xporter_1" in controller._pending
            assert id(controller._pending["xporter_1"]) == wf_pending_obj_id
            assert controller._pending["xporter_1"].command_id == wf_command_id

        # Clean up the workflow command.
        await controller.handle_response(ResponseMessage(
            command_id=wf_command_id, success=True, result={"ok": True},
            error=None, error_type=None,
        ))
        await wf_task

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_times_out_via_wait_for_fallback(
        self, mock_registry, mock_conn_manager, controller
    ):
        """World-sync op uses bounded ``asyncio.wait_for`` since it has no command_timer."""
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        with pytest.raises(CommandTimeoutError, match="World-sync command"):
            await controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=0.2, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_send_failure_does_not_clobber_workflow_pending(
        self, mock_registry, mock_conn_manager, controller
    ):
        """Early-failure path of world-sync (send_to_client returns False)
        raises DeviceOfflineError. The finally block must NOT pop the
        in-flight workflow command's ``_pending`` entry on the same
        device. Regression for the clobber risk on the early-exception
        path.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        # First send (workflow) succeeds, second send (world-sync) fails.
        mock_conn_manager.send_to_client = AsyncMock(side_effect=[True, False])

        # Start a workflow command; let it park in _pending.
        wf_task = asyncio.create_task(
            controller.execute_command(
                "xporter_1", "home", {}, timeout_seconds=10,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(lambda: "xporter_1" in controller._pending)
        async with controller._lock:
            assert "xporter_1" in controller._pending
            wf_command_id = controller._pending["xporter_1"].command_id
            wf_pending_obj_id = id(controller._pending["xporter_1"])

        # Now fire a world-sync op whose send_to_client returns False.
        with pytest.raises(DeviceOfflineError):
            await controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=5, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )

        # Workflow command's bookkeeping must still be intact.
        async with controller._lock:
            assert "xporter_1" in controller._pending
            assert controller._pending["xporter_1"].command_id == wf_command_id
            assert id(controller._pending["xporter_1"]) == wf_pending_obj_id

        # Clean up.
        await controller.handle_response(ResponseMessage(
            command_id=wf_command_id, success=True, result={"ok": True},
            error=None, error_type=None,
        ))
        await wf_task

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_cleans_up_command_futures_on_success_and_timeout(
        self, mock_registry, mock_conn_manager, controller
    ):
        """``_command_futures`` keyed by command_id is always cleaned up
        for world-sync, on both happy-path and timeout-path.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )
        mock_registry.is_device_online = AsyncMock(return_value=True)
        mock_registry.get_client_for_device = AsyncMock(return_value="client_1")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        # Happy path.
        task = asyncio.create_task(
            controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=5, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )
        )
        await wait_until(lambda: len(controller._command_futures) > 0)
        async with controller._lock:
            command_id = next(iter(controller._command_futures))
        await controller.handle_response(ResponseMessage(
            command_id=command_id, success=True, result={}, error=None, error_type=None,
        ))
        await task
        async with controller._lock:
            assert command_id not in controller._command_futures

        # Timeout path.
        with pytest.raises(CommandTimeoutError):
            await controller.execute_command(
                "xporter_1", "ensure_seeded", _WORLD_SYNC_PARAMS,
                timeout_seconds=0.1, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )
        # After timeout, no leaked _command_futures for this device's wire op.
        async with controller._lock:
            assert len(controller._command_futures) == 0

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_world_sync_still_runs_payload_validation(
        self, mock_registry, mock_conn_manager, controller
    ):
        """The bypass only skips the busy check, NOT payload validation.

        A malformed ``ensure_seeded`` params dict must still raise
        ``InvalidCommandError`` before any wire send, regardless of
        ``CommandKind.WORLD_SYNC``.
        """
        mock_registry.get_device = AsyncMock(
            return_value=_snapshot("xporter_1", type="transporter", interfaces=["ITransporter"]),
        )

        with pytest.raises(InvalidCommandError, match="Invalid params"):
            await controller.execute_command(
                "xporter_1", "ensure_seeded",
                {"position_id": "pos_1"},  # missing required 'labware'
                timeout_seconds=5, kind=CommandKind.WORLD_SYNC,
                effective_mode=WorkflowRunMode.LIVE,
            )


@pytest.mark.asyncio
class TestArmCommandTimerGuard:
    """``_arm_command_timer`` must never create a second live timer.

    The command-timer is armed only after a successful dispatch. If a
    disconnect swapped in a ``disconnect_timer`` while the dispatch was in
    flight, arming a command_timer too would put both timers live and break
    the disconnect/reconnect state machine's mutual-exclusion invariant.
    """

    async def test_arms_command_timer_when_no_disconnect_timer(self, controller):
        pending = _busy_pending("device_1", "cmd_1")
        async with controller._lock:
            controller._pending["device_1"] = pending

        await controller._arm_command_timer("device_1")

        assert pending.command_timer is not None
        pending.command_timer.cancel()

    async def test_skips_arming_when_disconnect_timer_already_set(self, controller):
        pending = _busy_pending("device_1", "cmd_1")
        sentinel = asyncio.create_task(asyncio.sleep(3600))
        pending.disconnect_timer = sentinel
        async with controller._lock:
            controller._pending["device_1"] = pending

        await controller._arm_command_timer("device_1")

        assert pending.command_timer is None  # not double-armed
        sentinel.cancel()

    async def test_skips_arming_when_future_already_done(self, controller):
        pending = _busy_pending("device_1", "cmd_1")
        pending.future.set_result({"ok": True})
        async with controller._lock:
            controller._pending["device_1"] = pending

        await controller._arm_command_timer("device_1")

        assert pending.command_timer is None
