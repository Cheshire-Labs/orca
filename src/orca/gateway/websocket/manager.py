"""WebSocket connection manager for device-bridge connections.

Owns the connection state map (connect / disconnect / send / heartbeat
update). A socket the server gives up on, after a failed send or missed
heartbeats, is only closed here; the router's cleanup for that socket is the
one place a client is removed. The periodic sweep lives on
:class:`WireHealthMonitor` in ``health_monitor.py``.

Each :class:`ConnectionState` tracks the devices attached to the client so
the disconnect path can fire `device.disconnected` per device without
re-reading the DB.
"""

from typing import Dict, List, Optional, Protocol
from datetime import datetime, timedelta, timezone
from fastapi import WebSocket
import asyncio
import logging


logger = logging.getLogger(__name__)


class ConnectionState:
    """State for a single device-bridge WebSocket connection."""

    def __init__(
        self,
        websocket: WebSocket,
        client_id: str,
        site: str,
        lab: str,
        workcell: Optional[str],
    ):
        self.websocket = websocket
        self.client_id = client_id
        self.site = site
        self.lab = lab
        self.workcell = workcell
        self.last_heartbeat = datetime.now(timezone.utc)
        self.closing = False
        # Device IDs registered for this client. Set by the router
        # right after `ConnectionManager.connect`, so the disconnect path
        # can fire per-device events without re-reading the DB.
        self.device_ids: List[str] = []

    def update_heartbeat(self) -> None:
        """Update last heartbeat timestamp."""
        self.last_heartbeat = datetime.now(timezone.utc)

    def is_stale(self, timeout_seconds: float) -> bool:
        """Whether no heartbeat arrived in the last `timeout_seconds`."""
        threshold = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        return self.last_heartbeat < threshold


async def _close(conn: ConnectionState, code: int, reason: str) -> None:
    """Close a socket once, so the router's receive loop ends and its cleanup runs."""
    if conn.closing:
        return
    conn.closing = True
    try:
        await conn.websocket.close(code=code, reason=reason)
    except Exception as e:
        logger.debug(f"Error closing connection {conn.client_id}: {e}")


class IClientChannel(Protocol):
    """Sends a message to a connected orca-client by its id. What the controller and the assignments need."""

    def connected_client_ids(self) -> List[str]: ...

    async def send_to_client(self, client_id: str, message: str) -> bool: ...


class IConnectionManager(IClientChannel, Protocol):
    """The open device-bridge sockets, keyed by client id."""

    async def connect(
        self,
        websocket: WebSocket,
        client_id: str,
        site: str,
        lab: str,
        workcell: Optional[str],
    ) -> List[str]: ...

    async def attach_devices(self, client_id: str, device_ids: List[str]) -> None: ...

    async def disconnect(self, client_id: str, websocket: WebSocket) -> Optional[List[str]]: ...

    async def get_connection(self, client_id: str) -> Optional[ConnectionState]: ...

    async def update_heartbeat(self, client_id: str) -> None: ...

    async def close_stale_connections(self, timeout_seconds: float) -> List[str]: ...


class ConnectionManager:
    """
    Manages active WebSocket connections from device-bridge instances.

    Tracks connection state, handles heartbeat monitoring, and provides
    methods to send commands to specific clients.
    """

    def __init__(self):
        """Initialize the connection manager."""
        self._connections: Dict[str, ConnectionState] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        websocket: WebSocket,
        client_id: str,
        site: str,
        lab: str,
        workcell: Optional[str],
    ) -> List[str]:
        """Register a new WebSocket connection.

        Returns the device_ids that were attached to a previously-existing
        connection for this client_id, so the caller can fire
        `device.disconnected` per device for the displaced connection.
        Empty list when there was no prior connection.
        """
        displaced: List[str] = []
        async with self._lock:
            if client_id in self._connections:
                old_conn = self._connections[client_id]
                displaced = list(old_conn.device_ids)
                try:
                    await old_conn.websocket.close(code=1000, reason="Reconnecting")
                except Exception as e:
                    logger.warning(f"Error closing old connection: {e}")

            self._connections[client_id] = ConnectionState(
                websocket=websocket,
                client_id=client_id,
                site=site,
                lab=lab,
                workcell=workcell,
            )

            logger.info(
                f"Client connected: {client_id} (site={site}, lab={lab}, workcell={workcell})"
            )
        return displaced

    async def attach_devices(self, client_id: str, device_ids: List[str]) -> None:
        """Record which devices belong to a client.

        The disconnect path uses this list to emit `device.disconnected`
        per device without re-reading the DB. Called by the router right
        after `_register_devices` succeeds.
        """
        async with self._lock:
            conn = self._connections.get(client_id)
            if conn is not None:
                conn.device_ids = list(device_ids)

    async def disconnect(self, client_id: str, websocket: WebSocket) -> Optional[List[str]]:
        """
        Unregister a WebSocket connection, if `websocket` is still the client's.

        A closed socket must not delete the newer registration that replaced it.

        Returns:
            The device_ids that were attached to this client, so the
            caller can fire `device.disconnected` per device. None when
            `websocket` is not the client's registered connection.
        """
        async with self._lock:
            conn = self._connections.get(client_id)
            if conn is None or conn.websocket is not websocket:
                return None
            del self._connections[client_id]
            logger.info(f"Client disconnected: {client_id}")
            return list(conn.device_ids)

    async def get_connection(self, client_id: str) -> Optional[ConnectionState]:
        """
        Get connection state for a client.

        Args:
            client_id: Client to lookup

        Returns:
            ConnectionState or None if not connected
        """
        async with self._lock:
            return self._connections.get(client_id)

    async def send_to_client(self, client_id: str, message: str) -> bool:
        """Send a message to a client. False when it is not connected or the send failed.

        A failed send closes the socket. The router's cleanup for that socket then unregisters the client
        and reports its devices disconnected, as for any other disconnect.
        """
        conn = await self.get_connection(client_id)
        if conn is None:
            return False

        try:
            await conn.websocket.send_text(message)
            return True
        except Exception as e:
            logger.error(f"Error sending to client {client_id}: {e}")
            await _close(conn, 1011, "Send failed")
            return False

    async def update_heartbeat(self, client_id: str) -> None:
        """
        Update heartbeat timestamp for a client.

        Args:
            client_id: Client that sent heartbeat
        """
        async with self._lock:
            if client_id in self._connections:
                self._connections[client_id].update_heartbeat()

    async def close_stale_connections(self, timeout_seconds: float) -> List[str]:
        """Close each connection that missed its heartbeats, and return their client ids.

        The router's cleanup for each socket unregisters the client and reports its devices disconnected.
        """
        async with self._lock:
            stale = [conn for conn in self._connections.values() if conn.is_stale(timeout_seconds)]
        for conn in stale:
            logger.warning(f"Closing stale connection: {conn.client_id}")
        # Together: a dead peer can take tens of seconds to close, and the next socket must not wait for it.
        await asyncio.gather(*(_close(conn, 1000, "Heartbeat timeout") for conn in stale))
        return [conn.client_id for conn in stale]

    def connected_client_ids(self) -> List[str]:
        return list(self._connections)

    def get_active_connection_count(self) -> int:
        """Get number of active connections."""
        return len(self._connections)

