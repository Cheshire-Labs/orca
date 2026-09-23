"""Tests for F2a: liquid handlers opt out of reconnect resend.

The device gateway's default disconnect-recovery policy resends the
in-flight command after a transient WS drop and reconnect. This is safe
for most drivers (the disconnect almost always means the device wasn't
reached), but is dangerous for non-idempotent liquid-handler commands
(aspirate/dispense) where partial-channel state is invisible from the
wire. F2a wires a per-command `resend_on_reconnect` flag so liquid
handlers fail the pending command with a clear DeviceOfflineError
instead of re-sending. Other drivers keep the existing resend behavior.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from orca.runtime.run_modes import WorkflowRunMode

from orca.gateway.controller import DeviceController, DeviceOfflineError
from orca.gateway.controller.controller import _PendingCommand
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from orca.gateway.remote_drivers import (
    RemoteLiquidHandlerDriver,
    RemoteShakerDriver,
    _RemoteDriverBase,
)
from orca.gateway.remote_transporter_driver import RemoteTransporterDriver


def _grace_with_resolver(resolver) -> DisconnectGrace:
    grace = DisconnectGrace()
    grace.set_topology_resolver(resolver)
    return grace


def _make_pending(
    device_id: str = "lh_1",
    command: str = "aspirate",
    resend_on_reconnect: bool = True,
) -> _PendingCommand:
    return _PendingCommand(
        command_id="cmd_1",
        device_id=device_id,
        command=command,
        params={},
        effective_mode=WorkflowRunMode.LIVE,
        timeout_seconds=30.0,
        future=asyncio.Future(),
        resend_on_reconnect=resend_on_reconnect,
    )


@pytest.fixture
def controller() -> DeviceController:
    return DeviceController()


class TestResendOnReconnectClassFlag:
    """Driver classes declare their resend policy as a ClassVar."""

    def test_remote_driver_base_default_is_true(self) -> None:
        assert _RemoteDriverBase.resend_on_reconnect is True

    def test_remote_shaker_inherits_default_true(self) -> None:
        assert RemoteShakerDriver.resend_on_reconnect is True

    def test_remote_transporter_default_is_true(self) -> None:
        assert RemoteTransporterDriver.resend_on_reconnect is True

    def test_remote_liquid_handler_overrides_to_false(self) -> None:
        assert RemoteLiquidHandlerDriver.resend_on_reconnect is False


@pytest.mark.asyncio
class TestOnDeviceReconnectedRespectsFlag:
    """On reconnect, controller honors the per-command resend flag."""

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_lh_command_fails_with_device_offline_instead_of_resending(
        self,
        mock_registry: Mock,
        mock_conn_manager: Mock,
        controller: DeviceController,
    ) -> None:
        """LH aspirate that disconnects mid-flight MUST NOT resend on reconnect.

        The pending future fails with DeviceOfflineError. The error
        message identifies the LH-no-resend policy so operators know
        manual recovery is required.
        """
        pending = _make_pending(
            device_id="lh_1", command="aspirate", resend_on_reconnect=False,
        )
        controller._pending["lh_1"] = pending

        async def resolver(_device_id: str) -> float | None:
            return 60.0
        controller.set_disconnect_grace(_grace_with_resolver(resolver))
        await controller.on_device_disconnected("lh_1")
        prior_timer = pending.disconnect_timer
        assert prior_timer is not None

        # Reconnect plumbing -- a new client is available, but the
        # controller MUST NOT resend the LH command.
        mock_registry.get_client_for_device = AsyncMock(return_value="client_2")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        await controller.on_device_reconnected("lh_1")
        await asyncio.sleep(0)

        assert prior_timer.cancelled() or prior_timer.done()
        # No resend on the wire.
        mock_conn_manager.send_to_client.assert_not_called()
        # Future fails with DeviceOfflineError carrying the LH-no-resend message.
        assert pending.future.done()
        exc = pending.future.exception()
        assert isinstance(exc, DeviceOfflineError)
        assert "manual recovery" in str(exc).lower()
        # Pending entry is cleaned up so a later operator retry isn't
        # blocked by stale state.
        assert "lh_1" not in controller._pending

    @patch("orca.gateway.controller.controller.connection_manager")
    @patch("orca.gateway.controller.controller.device_connection_tracker")
    async def test_shaker_command_still_resends_on_reconnect(
        self,
        mock_registry: Mock,
        mock_conn_manager: Mock,
        controller: DeviceController,
    ) -> None:
        """Non-LH drivers keep the existing resend-on-reconnect behavior.

        Regression guard: F2a must not change the recovery path for
        shakers, centrifuges, transporters, etc. -- those commands are
        either fully idempotent (set_speed) or so coarse that resend on
        reconnect is the right move.
        """
        pending = _make_pending(
            device_id="shaker_1", command="shake", resend_on_reconnect=True,
        )
        controller._pending["shaker_1"] = pending

        async def resolver(_device_id: str) -> float | None:
            return 60.0
        controller.set_disconnect_grace(_grace_with_resolver(resolver))
        await controller.on_device_disconnected("shaker_1")
        prior_timer = pending.disconnect_timer
        assert prior_timer is not None

        mock_registry.get_client_for_device = AsyncMock(return_value="client_2")
        mock_conn_manager.send_to_client = AsyncMock(return_value=True)

        await controller.on_device_reconnected("shaker_1")
        await asyncio.sleep(0)

        assert prior_timer.cancelled() or prior_timer.done()
        # Resend went out on the wire.
        mock_conn_manager.send_to_client.assert_awaited_once()
        # Future remains pending; the post-reconnect command is in
        # flight again.
        assert not pending.future.done()
        # New command timer armed.
        assert pending.command_timer is not None

        # Cleanup.
        if pending.command_timer is not None:
            pending.command_timer.cancel()
