"""No command is sent a second time when a device's orca-client reconnects, whatever the device is.

The device may have run the command before the connection dropped, and only a person at the device can say.
The command in flight fails as device offline, and the caller decides what to do next.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.gateway.controller import DeviceOfflineError
from orca.gateway.controller.controller import _PendingCommand
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from tests.gateway_parts import new_controller


def _make_pending(device_id: str, command: str) -> _PendingCommand:
    return _PendingCommand(
        command_id="cmd_1",
        device_id=device_id,
        command=command,
        timeout_seconds=30.0,
        future=asyncio.Future(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("device_id", "command"), [
    ("lh_1", "aspirate"),
    ("arm_1", "pick_at_coords"),
    ("shaker_1", "shake"),
    ("sealer_1", "seal"),
    ("reader_1", "read"),
])
async def test_a_command_in_flight_fails_as_offline_and_is_not_sent_again(device_id: str, command: str) -> None:
    mock_registry = MagicMock()
    mock_conn_manager = MagicMock()
    controller = new_controller(mock_registry, mock_conn_manager, disconnect_grace=DisconnectGrace(seconds=60.0))
    pending = _make_pending(device_id, command)
    controller._pending[device_id] = pending
    await controller.on_device_disconnected(device_id)
    prior_timer = pending.disconnect_timer
    assert prior_timer is not None

    mock_registry.get_client_for_device = AsyncMock(return_value="client_2")
    mock_conn_manager.send_to_client = AsyncMock(return_value=True)

    await controller.on_device_reconnected(device_id)
    await asyncio.sleep(0)

    assert prior_timer.cancelled() or prior_timer.done()
    mock_conn_manager.send_to_client.assert_not_called()
    exc = pending.future.exception()
    assert isinstance(exc, DeviceOfflineError)
    assert "manual recovery" in str(exc).lower()
    # Dropped, so the operator's next command to this device is not refused as busy.
    assert device_id not in controller._pending
    assert pending.command_timer is None
