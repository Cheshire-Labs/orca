"""WebSocket connection manager for device-bridge connections.

Owns the connection state map (connect / disconnect / send / heartbeat
update) and the prune call that drops stale entries. The periodic sweep
that drives the prune lives on :class:`WireHealthMonitor` in
``health_monitor.py`` -- ConnectionManager exposes the stateful
``cleanup_stale_connections`` call but does not own the cadence.

Each :class:`ConnectionState` tracks the devices attached to the client so
the disconnect path can fire `device.disconnected` per device via
`connection_events` without re-reading the DB.
"""

from typing import Dict, List, Optional, Tuple
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
        # Device IDs registered for this client. Set by the router
        # right after `connection_manager.connect`, so the disconnect path
        # can fire per-device events without re-reading the DB.
        self.device_ids: List[str] = []

    def update_heartbeat(self) -> None:
        """Update last heartbeat timestamp."""
        self.last_heartbeat = datetime.now(timezone.utc)

    def is_stale(self, timeout_seconds: int = 90) -> bool:
        """
        Check if connection is stale (missed heartbeats).

        Args:
            timeout_seconds: Heartbeat timeout threshold (default 90s)

        Returns:
            True if connection hasn't sent heartbeat within timeout period
        """
        threshold = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        return self.last_heartbeat < threshold


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

    async def disconnect(self, client_id: str) -> List[str]:
        """
        Unregister a WebSocket connection.

        Args:
            client_id: Client to disconnect

        Returns:
            The device_ids that were attached to this client, so the
            caller can fire `device.disconnected` per device. Empty list
            when no connection existed.
        """
        async with self._lock:
            conn = self._connections.pop(client_id, None)
            if conn is None:
                return []
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
        """
        Send a message to a specific client.

        Args:
            client_id: Client to send to
            message: JSON string message to send

        Returns:
            True if sent successfully, False if client not connected
        """
        conn = await self.get_connection(client_id)
        if conn is None:
            return False

        try:
            await conn.websocket.send_text(message)
            return True
        except Exception as e:
            logger.error(f"Error sending to client {client_id}: {e}")
            # Connection broken: drop it AND close the socket so the router's
            # receive_text() unwinds promptly instead of blocking forever.
            await self.disconnect(client_id)
            try:
                await conn.websocket.close(code=1011, reason="Send failed")
            except Exception as close_err:
                logger.debug(
                    f"Error closing broken connection {client_id}: {close_err}"
                )
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

    async def cleanup_stale_connections(
        self, timeout_seconds: int = 90,
    ) -> List[Tuple[str, List[str]]]:
        """Remove stale connections (missed heartbeats).

        Returns one ``(client_id, device_ids)`` pair per dropped client,
        so the heartbeat sweep can fire `device.disconnected` per device
        for every stale client. Callers that only care about the count
        should take ``len(...)`` on the return.
        """
        dropped: List[Tuple[str, List[str]]] = []
        async with self._lock:
            stale_clients = [
                client_id
                for client_id, conn in self._connections.items()
                if conn.is_stale(timeout_seconds)
            ]

            for client_id in stale_clients:
                conn = self._connections[client_id]
                try:
                    await conn.websocket.close(code=1000, reason="Heartbeat timeout")
                except Exception as e:
                    logger.warning(f"Error closing stale connection {client_id}: {e}")

                dropped.append((client_id, list(conn.device_ids)))
                del self._connections[client_id]
                logger.warning(f"Removed stale connection: {client_id}")

        return dropped

    def get_active_connection_count(self) -> int:
        """Get number of active connections."""
        return len(self._connections)


# Global connection manager instance
connection_manager = ConnectionManager()
