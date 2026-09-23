"""The daemon lifespan runs the stale-connection sweep for /ws/devices.

Without this wiring a missed-heartbeat connection is never pruned and
device.disconnected never fires for a dead device bridge. These tests pin both
halves: the lifespan starts/stops the monitor, and the on-dropped callback
emits device.disconnected per device on the stale client.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from orca.daemon.app import _daemon_lifespan, _emit_dropped_disconnects, create_app
from orca.gateway.websocket.connection_events import connection_events
from orca.gateway.websocket.manager import connection_manager


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_the_monitor() -> None:
    app = create_app()
    async with _daemon_lifespan(app):
        monitor = app.state.wire_health_monitor
        assert monitor is not None
        assert monitor.is_running
    # After the lifespan exits the sweep must be stopped.
    assert not monitor.is_running


@pytest.mark.asyncio
async def test_dropped_callback_emits_device_disconnected_per_device() -> None:
    seen: list[str] = []

    async def listener(device_id: str) -> None:
        seen.append(device_id)

    connection_events.subscribe_disconnected(listener)
    try:
        await _emit_dropped_disconnects(
            [("lab1-client", ["shaker_1", "centrifuge_1"]),
             ("lab2-client", ["reader_1"])],
        )
    finally:
        connection_events.unsubscribe_disconnected(listener)

    assert seen == ["shaker_1", "centrifuge_1", "reader_1"]


@pytest.mark.asyncio
async def test_stale_connection_is_pruned_and_fires_disconnect_under_lifespan() -> None:
    """End to end against the module singletons: a stale connection is
    pruned by the running sweep and device.disconnected fires for its device.
    """
    seen: list[str] = []

    async def listener(device_id: str) -> None:
        seen.append(device_id)

    ws = Mock()
    ws.close = AsyncMock()
    await connection_manager.connect(
        websocket=ws,
        client_id="lab1-client",
        site="boston", lab="molbio", workcell=None,
    )
    await connection_manager.attach_devices("lab1-client", ["shaker_1"])
    conn = await connection_manager.get_connection("lab1-client")
    assert conn is not None
    conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=300)

    connection_events.subscribe_disconnected(listener)
    app = create_app()
    try:
        async with _daemon_lifespan(app):
            monitor = app.state.wire_health_monitor
            # Drive a single sweep tick directly rather than waiting on the
            # 30s default interval. Same prune the periodic loop performs.
            dropped = await connection_manager.cleanup_stale_connections(
                timeout_seconds=monitor.timeout_seconds,
            )
            await _emit_dropped_disconnects(dropped)
    finally:
        connection_events.unsubscribe_disconnected(listener)
        await connection_manager.disconnect("lab1-client")

    assert seen == ["shaker_1"]
    assert await connection_manager.get_connection("lab1-client") is None
