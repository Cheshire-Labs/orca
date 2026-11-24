"""The daemon lifespan runs the stale-connection sweep for /ws/devices.

Without this wiring a missed-heartbeat connection is never closed, so the
router never cleans it up and device.disconnected never fires for a dead
device bridge.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from orca.daemon.app import _daemon_lifespan, create_app
from orca.gateway.websocket.manager import ConnectionManager


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_the_monitor() -> None:
    app = create_app()
    async with _daemon_lifespan(app.state.daemon)(app):
        monitor = app.state.wire_health_monitor
        assert monitor is not None
        assert monitor.is_running
    # After the lifespan exits the sweep must be stopped.
    assert not monitor.is_running


@pytest.mark.asyncio
async def test_the_running_sweep_closes_a_stale_connection() -> None:
    """End to end against the daemon's own gateway: the sweep closes a stale socket, so its router cleans up."""
    app = create_app()
    connection_manager = app.state.daemon.gateway.sockets
    assert isinstance(connection_manager, ConnectionManager)
    ws = Mock()
    ws.close = AsyncMock()
    await connection_manager.connect(
        websocket=ws,
        client_id="lab1-client",
        site="boston", lab="molbio", workcell=None,
    )
    conn = await connection_manager.get_connection("lab1-client")
    assert conn is not None
    conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=300)

    try:
        async with _daemon_lifespan(app.state.daemon)(app):
            monitor = app.state.wire_health_monitor
            # One tick directly rather than waiting on the default interval.
            closed = await connection_manager.close_stale_connections(
                timeout_seconds=monitor.timeout_seconds,
            )
    finally:
        await connection_manager.disconnect("lab1-client", ws)

    assert closed == ["lab1-client"]
    ws.close.assert_awaited_once_with(code=1000, reason="Heartbeat timeout")
