"""A client that stops heartbeating is offline, may be taken over, and has its socket closed, all by the same deadline."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from cheshire_drivers.gateway_protocol import HEARTBEAT_INTERVAL_SECONDS, DeviceConnectInfo

from orca.gateway.connection_source import DeviceConnectionSource
from orca.gateway.registry.connection_tracker import HEARTBEAT_TOLERANCE_SECONDS, DeviceConnectionTracker
from orca.gateway.websocket.health_monitor import WireHealthMonitor
from orca.gateway.websocket.manager import ConnectionManager, ConnectionState

_SHAKER = DeviceConnectInfo.model_validate({"type": "shaker", "name": "shaker_1", "interfaces": ["IShaker"]})


class _HalfOpenClient:
    """One registered client whose socket is still open but sends nothing."""

    def __init__(self) -> None:
        self.tracker = DeviceConnectionTracker()
        self.sockets = ConnectionManager()
        self.source = DeviceConnectionSource(self.tracker)
        self.websocket = Mock()
        self.websocket.close = AsyncMock()

    async def connect(self) -> ConnectionState:
        await self.sockets.connect(websocket=self.websocket, client_id="lab_pc", site="s", lab="l", workcell=None)
        await self.tracker.register_client(client_id="lab_pc", site="s", lab="l", workcell=None, devices=[_SHAKER])
        connection = await self.sockets.get_connection("lab_pc")
        assert connection is not None
        return connection

    def last_heard(self, connection: ConnectionState, seconds_ago: float) -> None:
        heard = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        connection.last_heartbeat = heard
        self.tracker._devices["shaker_1"].snapshot.last_seen = heard


@pytest.mark.asyncio
async def test_one_missed_heartbeat_neither_closes_the_socket_nor_marks_the_device_offline() -> None:
    client = _HalfOpenClient()
    connection = await client.connect()
    one_missed_and_the_next_late = 2 * HEARTBEAT_INTERVAL_SECONDS + 10
    client.last_heard(connection, one_missed_and_the_next_late)
    monitor = WireHealthMonitor(client.sockets)

    assert await client.source.is_connected("shaker_1") is True
    assert not connection.is_stale(HEARTBEAT_TOLERANCE_SECONDS), "a second connection could take the client over"
    assert await client.sockets.close_stale_connections(timeout_seconds=monitor.timeout_seconds) == []
    client.websocket.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_once_the_registry_calls_it_offline_it_may_be_taken_over_and_the_sweep_closes_it() -> None:
    client = _HalfOpenClient()
    connection = await client.connect()
    client.last_heard(connection, HEARTBEAT_TOLERANCE_SECONDS + 0.5)
    monitor = WireHealthMonitor(client.sockets)

    assert await client.source.is_connected("shaker_1") is False
    assert connection.is_stale(HEARTBEAT_TOLERANCE_SECONDS), "a second connection could not take the client over"
    assert await client.sockets.close_stale_connections(timeout_seconds=monitor.timeout_seconds) == ["lab_pc"]


@pytest.mark.asyncio
async def test_a_socket_one_sweep_just_missed_is_closed_by_the_next_before_the_registry_calls_it_offline() -> None:
    client = _HalfOpenClient()
    connection = await client.connect()
    monitor = WireHealthMonitor(client.sockets)
    just_missed = monitor.timeout_seconds - 0.5
    client.last_heard(connection, just_missed)
    assert await client.sockets.close_stale_connections(timeout_seconds=monitor.timeout_seconds) == []

    age_at_next_sweep = just_missed + monitor.interval_seconds
    client.last_heard(connection, age_at_next_sweep)

    assert await client.source.is_connected("shaker_1") is True, "the registry called it offline while its socket was open"
    assert await client.sockets.close_stale_connections(timeout_seconds=monitor.timeout_seconds) == ["lab_pc"]


@pytest.mark.asyncio
async def test_a_peer_slow_to_close_does_not_hold_back_closing_the_next_one() -> None:
    sockets = ConnectionManager()
    second_closing = asyncio.Event()

    async def slow_close(code: int, reason: str) -> None:
        await second_closing.wait()

    async def close(code: int, reason: str) -> None:
        second_closing.set()

    for client_id, closer in (("slow_pc", slow_close), ("lab_pc", close)):
        websocket = Mock()
        websocket.close = closer
        await sockets.connect(websocket=websocket, client_id=client_id, site="s", lab="l", workcell=None)
        connection = await sockets.get_connection(client_id)
        assert connection is not None
        connection.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_TOLERANCE_SECONDS)

    closed = await asyncio.wait_for(sockets.close_stale_connections(timeout_seconds=HEARTBEAT_TOLERANCE_SECONDS - 1), timeout=5)

    assert sorted(closed) == ["lab_pc", "slow_pc"]
