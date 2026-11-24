"""Tracker of currently-connected device sessions.

:class:`DeviceConnectionTracker` is the hot, in-memory roster of devices
that device bridges have advertised over their open WebSocket. Reads are
authoritative for "is this device reachable right now"; there is no
cold storage. The connection state is purely in-memory, and on a fresh
process the tracker repopulates itself from the next ``AssignmentResult``.

Orca's ``IDeviceRegistry`` reads from this tracker via
:class:`orca.gateway.connection_source.DeviceConnectionSource` to
derive the "connection card" half of its two-card view. This tracker
is intentionally NOT the registry of devices that workflows reference.

Public read methods (:meth:`get_device`, :meth:`list_devices`) return typed
:class:`DeviceSnapshot` instances; ``client_id``
is internal-only and tracked alongside the snapshot in
:class:`_RegisteredDeviceState` (an internal-only state record) so it can
never leak through the public read path. Callers needing the owning client
id use :meth:`get_client_for_device`.

Devices are indexed by ``name`` -- the operator-visible identifier that
matches the topology declaration (``Shaker(name="shaker_1")``). Both the
wire-side ``DeviceConnectInfo.name`` and the orca-core topology resource
name are the same string, so the registry can compose the two-card view
by a name-keyed dict join with no translation layer.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Protocol

from cheshire_drivers.gateway_protocol import HEARTBEAT_INTERVAL_SECONDS, DeviceConnectInfo, DeviceStatusInfo
from orca.gateway.registry.snapshot import (
    DeviceSnapshot,
    _ClientState,
    _RegisteredDeviceState,
)

logger = logging.getLogger(__name__)

# How long a client may go unheard before it is gone: three heartbeat intervals, so one late or lost
# heartbeat costs nothing. The registry's "offline", the router's takeover and the stale-socket sweep all use it.
HEARTBEAT_TOLERANCE_SECONDS: float = 3 * HEARTBEAT_INTERVAL_SECONDS


class IDeviceConnectionTracker(Protocol):
    """The roster of devices the connected device bridges advertised."""

    async def register_client(
        self,
        client_id: str,
        site: str,
        lab: str,
        workcell: Optional[str],
        devices: List[DeviceConnectInfo],
    ) -> None: ...

    async def unregister_client(self, client_id: str) -> None: ...

    async def record_build_error(self, client_id: str, name: str, error: str) -> None: ...

    def build_error(self, name: str) -> Optional[str]: ...

    async def forget_devices(self, client_id: str, names: List[str]) -> List[str]: ...

    async def get_device(self, name: str) -> Optional[DeviceSnapshot]: ...

    async def get_client_for_device(self, name: str) -> Optional[str]: ...

    async def get_device_with_client(
        self, name: str,
    ) -> Optional[tuple[DeviceSnapshot, str]]: ...

    async def list_devices_with_clients(self) -> List[tuple[DeviceSnapshot, str]]: ...

    async def list_devices(
        self,
        device_type: Optional[str] = None,
        site: Optional[str] = None,
        lab: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[DeviceSnapshot]: ...

    async def apply_agent_report(self, client_id: str, name: str, reported: DeviceStatusInfo) -> bool: ...

    def peek_snapshot(self, name: str) -> Optional[DeviceSnapshot]: ...

    async def update_heartbeat(self, client_id: str) -> None: ...

    async def is_device_online(self, name: str) -> bool: ...


class DeviceConnectionTracker:
    """In-memory tracker of currently-connected device sessions.

    Tracks:
    - Active client connections (client_id -> client metadata)
    - Connected devices (name -> DeviceSnapshot + owning client_id)
    - Client-to-device mappings

    Storage:
    - Hot state (authoritative): In-memory dicts, fed by WebSocket events,
      consulted directly for command routing.
    - No cold storage: connection state is purely in-memory, repopulated
      from the next ``AssignmentResult`` on reconnect. orca-core has no
      database dependency.
    """

    def __init__(self) -> None:
        self._clients: Dict[str, _ClientState] = {}
        self._devices: Dict[str, _RegisteredDeviceState] = {}
        self._build_errors: Dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def register_client(
        self,
        client_id: str,
        site: str,
        lab: str,
        workcell: Optional[str],
        devices: List[DeviceConnectInfo],
    ) -> None:
        """Register a client and its devices.

        Each DeviceConnectInfo carries the device's identity plus the full
        capability advertisement (interfaces, auto-derived vendor extras,
        provides_state, and per-method introspection metadata) which the
        controller and introspection endpoints consult.
        """
        async with self._lock:
            now = datetime.now(timezone.utc)

            self._clients[client_id] = _ClientState(
                site=site,
                lab=lab,
                workcell=workcell,
                connected_at=now,
                last_heartbeat=now,
            )

            for info in devices:
                self._build_errors.pop(info.name, None)
                snapshot = DeviceSnapshot(
                    name=info.name,
                    type=info.type,
                    interfaces=sorted(info.interfaces),
                    capabilities=sorted(info.capabilities),
                    provides_state=info.provides_state,
                    methods=dict(info.methods),
                    site=site,
                    lab=lab,
                    workcell=workcell,
                    status="ready",
                    last_seen=datetime.now(timezone.utc),
                )
                self._devices[info.name] = _RegisteredDeviceState(
                    client_id=client_id, snapshot=snapshot,
                )

            logger.info(
                f"Registered client {client_id} with {len(devices)} devices "
                f"(site={site}, lab={lab}, workcell={workcell})"
            )

    async def record_build_error(self, client_id: str, name: str, error: str) -> None:
        """Why the client could not build this device, kept until a build succeeds or the client leaves."""
        async with self._lock:
            self._build_errors[name] = (client_id, error)

    def build_error(self, name: str) -> Optional[str]:
        recorded = self._build_errors.get(name)
        return None if recorded is None else recorded[1]

    async def forget_devices(self, client_id: str, names: List[str]) -> List[str]:
        """Take devices this client no longer runs off the roster, and return them. A device another client now holds stays."""
        async with self._lock:
            forgotten = [name for name in names if name in self._devices and self._devices[name].client_id == client_id]
            for name in forgotten:
                del self._devices[name]
            return forgotten

    async def unregister_client(
        self,
        client_id: str,
    ) -> None:
        """Unregister a client and mark its devices as offline."""
        async with self._lock:
            self._build_errors = {
                name: recorded for name, recorded in self._build_errors.items() if recorded[0] != client_id
            }
            names_to_remove: List[str] = [
                name for name, state in self._devices.items()
                if state.client_id == client_id
            ]

            for name in names_to_remove:
                del self._devices[name]

            if client_id in self._clients:
                del self._clients[client_id]

            logger.info(
                f"Unregistered client {client_id}, {len(names_to_remove)} devices offline"
            )

    async def get_device(self, name: str) -> Optional[DeviceSnapshot]:
        """Get the typed snapshot for ``name``, or ``None`` if not registered.

        ``client_id`` is intentionally excluded from the public snapshot;
        use :meth:`get_client_for_device` when routing commands to the
        owning device bridge.
        """
        async with self._lock:
            state = self._devices.get(name)
            if state is None:
                return None
            return state.snapshot

    async def get_client_for_device(self, name: str) -> Optional[str]:
        """Find which client controls a device by name."""
        async with self._lock:
            state = self._devices.get(name)
            return state.client_id if state else None

    async def get_device_with_client(
        self, name: str,
    ) -> Optional[tuple[DeviceSnapshot, str]]:
        """Atomic (snapshot, client_id) lookup for ConnectionCard construction.

        The two-call pattern (``get_device`` then ``get_client_for_device``)
        opens a race window: the device row can be removed by an
        ``unregister_client`` between the two lock acquisitions, forcing
        the caller to fabricate a placeholder ``client_id`` for a device
        it has already decided exists. This accessor returns both values
        under one lock acquisition so the caller never sees the
        inconsistent half-state.
        """
        async with self._lock:
            state = self._devices.get(name)
            if state is None:
                return None
            return state.snapshot, state.client_id

    async def list_devices_with_clients(self) -> List[tuple[DeviceSnapshot, str]]:
        """Atomic list of every (snapshot, client_id) pair under one lock.

        Used by ConnectionCard list construction: emitting per-row
        ``(get_device, get_client_for_device)`` pairs would race with
        unregister mid-iteration. See :meth:`get_device_with_client` for
        the per-name equivalent.
        """
        async with self._lock:
            return [(state.snapshot, state.client_id)
                    for state in self._devices.values()]

    async def list_devices(
        self,
        device_type: Optional[str] = None,
        site: Optional[str] = None,
        lab: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[DeviceSnapshot]:
        """List device snapshots with optional filtering."""
        async with self._lock:
            snapshots = [state.snapshot for state in self._devices.values()]

            if device_type:
                snapshots = [s for s in snapshots if s.type == device_type]
            if site:
                snapshots = [s for s in snapshots if s.site == site]
            if lab:
                snapshots = [s for s in snapshots if s.lab == lab]
            if status:
                snapshots = [s for s in snapshots if s.status == status]

            return snapshots

    async def apply_agent_report(self, client_id: str, name: str, reported: DeviceStatusInfo) -> bool:
        """Record what the device bridge sees on this device (StatusMessage), and say whether it was recorded.

        Only the client that owns the device is heard: a client the device moved away from may still report it.

        The device bridge owns the driver objects, so its report is the only
        truthful source for the device's own link state; the control plane's
        own proxy goes stale the moment a command bypasses it.

        The report carries one link per run mode, and this roster answers reads
        that name no mode, so it takes `observed_link` instead of resolving
        one. `link_mode` records which driver did answer.
        """
        observed = reported.observed_link
        async with self._lock:
            state = self._devices.get(name)
            if state is None or state.client_id != client_id:
                return False
            state.snapshot.status = reported.status
            state.snapshot.link_mode = observed.mode
            state.snapshot.is_connected = observed.is_connected
            state.snapshot.is_initialized = observed.is_initialized
            state.snapshot.last_seen = datetime.now(timezone.utc)
            return True

    def peek_snapshot(self, name: str) -> Optional[DeviceSnapshot]:
        """Synchronously read a device's current snapshot, or None.

        Callers read several fields off the returned row, so finding it
        atomically is not the whole question. What makes this safe is that the
        writers hold the event loop for the whole update: no `await` sits
        between the field assignments in :meth:`apply_agent_report`, and
        nothing reads a snapshot off a worker thread. Add an `await` inside
        that block, or move a snapshot read into `asyncio.to_thread`, and this
        starts returning halves of two different reports.
        """
        state = self._devices.get(name)
        if state is None:
            return None
        return state.snapshot

    async def update_heartbeat(self, client_id: str) -> None:
        """Refresh the client heartbeat AND every device's ``last_seen``.

        ``DeviceConnectionSource.is_connected`` compares per-device
        ``last_seen`` against ``HEARTBEAT_TOLERANCE_SECONDS``. Without this
        per-device fan-out, a device whose driver never pushes a StatusMessage
        reads offline once that tolerance passes after registration.
        """
        async with self._lock:
            if client_id not in self._clients:
                return
            now = datetime.now(timezone.utc)
            self._clients[client_id].last_heartbeat = now
            for state in self._devices.values():
                if state.client_id == client_id:
                    state.snapshot.last_seen = now

    async def is_device_online(self, name: str) -> bool:
        """Check if device is currently online."""
        async with self._lock:
            return name in self._devices

    def get_active_client_count(self) -> int:
        return len(self._clients)

    def get_active_device_count(self) -> int:
        return len(self._devices)

