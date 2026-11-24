"""Tests for the PAUSE_AND_WAIT(timeout) -> FAIL disconnect policy."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from orca.gateway.controller import (
    DeviceController,
    DeviceOfflineError,
)
from orca.gateway.controller.controller import _PendingCommand
from orca.daemon.app import create_app
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from cheshire_drivers.gateway_protocol import ResponseMessage
from orca.gateway.registry.snapshot import DeviceSnapshot
from unittest.mock import MagicMock
from tests.gateway_parts import GatewayParts, gateway_parts, new_controller


def _snapshot(
    device_id: str = "shaker_1",
    type: str = "shaker",
    interfaces: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> DeviceSnapshot:
    return DeviceSnapshot(
        type=type,
        name=device_id,
        interfaces=interfaces or ["IShaker"],
        capabilities=capabilities or [],
        provides_state=False,
        methods={},
        site="test", lab="test", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    )


@pytest.fixture
def parts() -> GatewayParts:
    return gateway_parts()


@pytest.fixture
def controller(parts: GatewayParts) -> DeviceController:
    return parts.controller


def _controller_with_grace(seconds: float) -> DeviceController:
    return new_controller(MagicMock(), MagicMock(), disconnect_grace=DisconnectGrace(seconds=seconds))


def test_the_daemon_controller_waits_the_300s_grace() -> None:
    controller = create_app().state.daemon.connections.gateway.controller
    assert isinstance(controller, DeviceController)
    assert controller._disconnect_grace.seconds == 300.0


def _make_pending(
    device_id: str = "shaker_1",
    timeout_seconds: float = 30.0,
) -> _PendingCommand:
    """Build a _PendingCommand with a fresh future + no timers running."""
    return _PendingCommand(
        command_id="cmd_1",
        device_id=device_id,
        command="shake",
        timeout_seconds=timeout_seconds,
        future=asyncio.Future(),
    )


@pytest.mark.asyncio
class TestOnDeviceDisconnected:
    async def test_no_pending_command_is_noop(self, controller: DeviceController):
        # Should not raise even when nothing is in-flight.
        await controller.on_device_disconnected("shaker_1")

    async def test_disconnect_arms_disconnect_timer(self):
        """When a command is pending, the disconnect listener arms a timer. A short grace lets it fire fast."""
        controller = _controller_with_grace(0.05)
        pending = _make_pending()
        controller._pending["shaker_1"] = pending

        await controller.on_device_disconnected("shaker_1")

        assert pending.disconnect_timer is not None
        # Wait for the timer to fire. After grace, the future fails with
        # DeviceOfflineError.
        with pytest.raises(DeviceOfflineError):
            await asyncio.wait_for(pending.future, timeout=1.0)

    async def test_double_disconnect_is_idempotent(self):
        controller = _controller_with_grace(5.0)
        pending = _make_pending()
        controller._pending["shaker_1"] = pending

        await controller.on_device_disconnected("shaker_1")
        first_timer = pending.disconnect_timer
        await controller.on_device_disconnected("shaker_1")

        # Same timer; not replaced. (Don't await it -- it would fire
        # DeviceOfflineError after 5s.)
        assert pending.disconnect_timer is first_timer
        # Cleanup so the test process doesn't have a runaway task.
        if pending.disconnect_timer is not None:
            pending.disconnect_timer.cancel()


@pytest.mark.asyncio
class TestOnDeviceReconnected:
    async def test_no_pending_is_noop(self, controller: DeviceController):
        await controller.on_device_reconnected("shaker_1")

    async def test_reconnect_without_prior_disconnect_is_noop(
        self, controller: DeviceController,
    ):
        """A connect signal that didn't follow a disconnect is just a regular connect."""
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        # Pending is in-flight but never disconnected.
        await controller.on_device_reconnected("shaker_1")

        # No state change.
        assert pending.disconnect_timer is None
        assert not pending.future.done()

    async def test_reconnect_fails_the_command_and_cancels_the_disconnect_timer(self):
        """A reconnect within the grace fails the command as offline and sends nothing."""
        mock_registry = MagicMock()
        mock_conn_manager = MagicMock()
        controller = new_controller(mock_registry, mock_conn_manager)
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        await controller.on_device_disconnected("shaker_1")
        prior_timer = pending.disconnect_timer
        assert prior_timer is not None

        mock_registry.get_client_for_device = AsyncMock(return_value="client_2")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        await controller.on_device_reconnected("shaker_1")
        # Yield the loop so the cancelled timer transitions to done().
        await asyncio.sleep(0)

        assert prior_timer.cancelled() or prior_timer.done()
        assert pending.disconnect_timer is None
        assert pending.command_timer is None
        mock_conn_manager.send_to_client.assert_not_called()
        assert isinstance(pending.future.exception(), DeviceOfflineError)

    async def test_reconnect_with_no_client_fails_the_command_too(self):
        """Nothing is sent on a reconnect, so it does not matter whether a client is registered yet."""
        mock_registry = MagicMock()
        mock_conn_manager = MagicMock()
        controller = new_controller(mock_registry, mock_conn_manager)
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        await controller.on_device_disconnected("shaker_1")

        mock_registry.get_client_for_device = AsyncMock(return_value=None)

        await controller.on_device_reconnected("shaker_1")

        mock_conn_manager.send_to_client.assert_not_called()
        assert isinstance(pending.future.exception(), DeviceOfflineError)


@pytest.mark.asyncio
class TestDisconnectThenResponse:
    async def test_a_response_after_the_disconnect_resolves_the_command_normally(self):
        controller = _controller_with_grace(0.05)
        pending = _make_pending()
        controller._pending["shaker_1"] = pending
        controller._command_futures["cmd_1"] = pending.future
        await controller.on_device_disconnected("shaker_1")

        await controller.handle_response(
            ResponseMessage(command_id="cmd_1", success=True, result={"ok": True}, error=None, error_type=None)
        )

        assert pending.future.result() == {"ok": True}
        timer = pending.disconnect_timer
        assert timer is not None
        await asyncio.wait_for(timer, timeout=1.0)
        assert pending.future.result() == {"ok": True}, "the grace running out replaced the response's result"
