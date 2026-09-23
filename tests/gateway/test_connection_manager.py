"""Tests for WebSocket connection manager."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timedelta, timezone

from orca.gateway.websocket.manager import ConnectionManager, ConnectionState


@pytest.fixture
def manager():
    """Create a fresh connection manager for each test."""
    return ConnectionManager()


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket."""
    ws = Mock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.mark.asyncio
class TestConnectionState:
    """Tests for ConnectionState class."""

    def test_connection_state_ctor_seeds_live_defaults(self, mock_websocket):
        """The ctor seeds a fresh aware heartbeat and an empty device list.

        Field echo-back is plain assignment with no logic; the real ctor
        behavior worth pinning is the derived state: a brand-new connection
        is heartbeat-fresh (not stale) and starts with no devices attached.
        """
        before = datetime.now(timezone.utc)
        conn = ConnectionState(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )
        after = datetime.now(timezone.utc)

        assert conn.last_heartbeat.tzinfo is not None
        assert before <= conn.last_heartbeat <= after
        assert conn.is_stale(timeout_seconds=90) is False
        assert conn.device_ids == []

    async def test_update_heartbeat_advances_timestamp(self, mock_websocket):
        """update_heartbeat must move the timestamp strictly forward.

        A frozen clock makes the assertion bite: a no-op update would leave
        the timestamp at the seeded value and fail the strict-greater check.
        """
        ticks = iter([
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
        ])

        with patch(
            "orca.gateway.websocket.manager.datetime",
            wraps=datetime,
        ) as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: next(ticks)
            conn = ConnectionState(
                websocket=mock_websocket,
                client_id="lab1-client",
                site="boston",
                lab="molbio",
                workcell=None,
            )
            initial_heartbeat = conn.last_heartbeat
            conn.update_heartbeat()

        assert conn.last_heartbeat > initial_heartbeat

    def test_is_stale(self, mock_websocket):
        """Test stale connection detection."""
        conn = ConnectionState(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        # Fresh connection not stale
        assert conn.is_stale(timeout_seconds=90) is False

        # Manually set old heartbeat
        conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=120)
        assert conn.is_stale(timeout_seconds=90) is True


@pytest.mark.asyncio
class TestConnectionManager:
    """Tests for ConnectionManager class."""

    async def test_connect(self, manager, mock_websocket):
        """Test connecting a client."""
        await manager.connect(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        assert manager.get_active_connection_count() == 1

        conn = await manager.get_connection("lab1-client")
        assert conn is not None
        assert conn.client_id == "lab1-client"

    async def test_disconnect(self, manager, mock_websocket):
        """Test disconnecting a client."""
        await manager.connect(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        # Verify connected
        assert manager.get_active_connection_count() == 1

        # Disconnect
        await manager.disconnect("lab1-client")

        # Verify disconnected
        assert manager.get_active_connection_count() == 0
        conn = await manager.get_connection("lab1-client")
        assert conn is None

    async def test_reconnect_closes_old_connection(self, manager):
        """Test that reconnecting closes old connection."""
        old_ws = Mock()
        old_ws.close = AsyncMock()

        new_ws = Mock()
        new_ws.close = AsyncMock()

        # Connect first time
        await manager.connect(
            websocket=old_ws,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        # Reconnect with new WebSocket
        await manager.connect(
            websocket=new_ws,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        # Old connection should be closed
        old_ws.close.assert_called_once()

        # New connection should be active
        conn = await manager.get_connection("lab1-client")
        assert conn.websocket == new_ws

    async def test_send_to_client_success(self, manager, mock_websocket):
        """Test sending message to connected client."""
        await manager.connect(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        success = await manager.send_to_client("lab1-client", "test message")
        assert success is True
        mock_websocket.send_text.assert_called_once_with("test message")

    async def test_send_to_client_not_connected(self, manager):
        """Test sending message to disconnected client."""
        success = await manager.send_to_client("nonexistent", "test message")
        assert success is False

    async def test_send_to_client_error_disconnects(self, manager):
        """Test that send errors trigger disconnect."""
        mock_ws = Mock()
        mock_ws.send_text = AsyncMock(side_effect=Exception("Connection broken"))

        await manager.connect(
            websocket=mock_ws,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        # Send should fail and disconnect
        success = await manager.send_to_client("lab1-client", "test message")
        assert success is False

        # Connection should be removed
        assert manager.get_active_connection_count() == 0

    async def test_send_failure_closes_websocket(self, manager):
        """A failed send must close the socket so the router's receive_text()
        unwinds instead of blocking forever on a half-dead connection."""
        mock_ws = Mock()
        mock_ws.send_text = AsyncMock(side_effect=Exception("Connection broken"))
        mock_ws.close = AsyncMock()

        await manager.connect(
            websocket=mock_ws,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        success = await manager.send_to_client("lab1-client", "test message")
        assert success is False
        mock_ws.close.assert_awaited_once()

    async def test_send_failure_swallows_close_error(self, manager):
        """A close that itself raises must not propagate; the send result is
        still False and the connection is still removed."""
        mock_ws = Mock()
        mock_ws.send_text = AsyncMock(side_effect=Exception("Connection broken"))
        mock_ws.close = AsyncMock(side_effect=Exception("already closed"))

        await manager.connect(
            websocket=mock_ws,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        success = await manager.send_to_client("lab1-client", "test message")
        assert success is False
        assert manager.get_active_connection_count() == 0

    async def test_update_heartbeat(self, manager, mock_websocket):
        """Test updating heartbeat for connected client."""
        await manager.connect(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        conn = await manager.get_connection("lab1-client")
        initial_heartbeat = conn.last_heartbeat

        await manager.update_heartbeat("lab1-client")

        conn = await manager.get_connection("lab1-client")
        assert conn.last_heartbeat >= initial_heartbeat

    async def test_cleanup_stale_connections(self, manager):
        """Test cleaning up stale connections."""
        mock_ws1 = Mock()
        mock_ws1.close = AsyncMock()

        mock_ws2 = Mock()
        mock_ws2.close = AsyncMock()

        # Connect two clients
        await manager.connect(
            websocket=mock_ws1,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        await manager.connect(
            websocket=mock_ws2,
            client_id="lab2-client",
            site="cambridge",
            lab="cellculture",
            workcell=None,
        )

        # Make first connection stale
        conn1 = await manager.get_connection("lab1-client")
        conn1.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=120)

        # Cleanup stale connections
        removed = await manager.cleanup_stale_connections(timeout_seconds=90)

        # One connection should be removed. The return type is
        # `list[tuple[client_id, device_ids]]` so the heartbeat
        # sweep can fire device.disconnected per device. The "one removed"
        # contract is preserved via len().
        assert len(removed) == 1
        assert removed[0][0] == "lab1-client"
        assert removed[0][1] == []  # No devices attached in this test
        assert manager.get_active_connection_count() == 1

        # Stale connection should be gone
        assert await manager.get_connection("lab1-client") is None

        # Fresh connection should still be active
        assert await manager.get_connection("lab2-client") is not None

    async def test_multiple_clients(self, manager):
        """Test managing multiple simultaneous clients."""
        ws1 = Mock()
        ws1.close = AsyncMock()

        ws2 = Mock()
        ws2.close = AsyncMock()

        await manager.connect(
            websocket=ws1,
            client_id="lab1-client",
            site="boston",
            lab="molbio",
            workcell=None,
        )

        await manager.connect(
            websocket=ws2,
            client_id="lab2-client",
            site="cambridge",
            lab="cellculture",
            workcell=None,
        )

        assert manager.get_active_connection_count() == 2

        # Both connections retrievable
        conn1 = await manager.get_connection("lab1-client")
        conn2 = await manager.get_connection("lab2-client")

        assert conn1.client_id == "lab1-client"
        assert conn2.client_id == "lab2-client"


@pytest.mark.asyncio
class TestConnectionManagerDeviceTracking:
    """Device tracking + heartbeat sweep + reconnect displacement."""

    async def test_attach_devices_records_ids_on_state(self, manager, mock_websocket):
        await manager.connect(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )

        await manager.attach_devices("lab1-client", ["shaker_1", "centrifuge_1"])

        conn = await manager.get_connection("lab1-client")
        assert conn.device_ids == ["shaker_1", "centrifuge_1"]

    async def test_disconnect_returns_attached_device_ids(self, manager, mock_websocket):
        await manager.connect(
            websocket=mock_websocket,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        await manager.attach_devices("lab1-client", ["shaker_1"])

        out = await manager.disconnect("lab1-client")

        assert out == ["shaker_1"]
        assert manager.get_active_connection_count() == 0

    async def test_disconnect_unknown_client_returns_empty(self, manager):
        assert await manager.disconnect("missing") == []

    async def test_reconnect_displaces_returns_old_device_ids(self, manager):
        old_ws = Mock()
        old_ws.close = AsyncMock()
        new_ws = Mock()
        new_ws.close = AsyncMock()

        await manager.connect(
            websocket=old_ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        await manager.attach_devices("lab1-client", ["shaker_1"])

        displaced = await manager.connect(
            websocket=new_ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )

        assert displaced == ["shaker_1"]
        # New state has no devices yet (router calls attach_devices next).
        conn = await manager.get_connection("lab1-client")
        assert conn.device_ids == []
        old_ws.close.assert_awaited()

    async def test_cleanup_returns_dropped_pairs_with_devices(self, manager):
        ws = Mock()
        ws.close = AsyncMock()

        await manager.connect(
            websocket=ws,
            client_id="lab1-client",
            site="boston", lab="molbio", workcell=None,
        )
        await manager.attach_devices("lab1-client", ["shaker_1", "centrifuge_1"])

        # Force stale.
        conn = await manager.get_connection("lab1-client")
        conn.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=300)

        dropped = await manager.cleanup_stale_connections(timeout_seconds=90)

        assert dropped == [("lab1-client", ["shaker_1", "centrifuge_1"])]
