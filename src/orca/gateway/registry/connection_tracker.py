"""Tracker of currently-connected device sessions.

:class:`DeviceConnectionTracker` is the hot, in-memory roster of devices
that device bridges have advertised over their open WebSocket. Reads are
authoritative for "is this device reachable right now"; there is no
cold storage. The connection state is purely in-memory, and on a fresh
process the tracker repopulates itself from the next ``ConnectMessage``.

The unified ``DeviceRegistry`` reads from this tracker via
:class:`orca.gateway.connection_source.DeviceConnectionSource` to
derive the "connection card" half of its two-card view. This tracker
is intentionally NOT the registry of devices that workflows reference.

Public read methods (:meth:`get_device`, :meth:`list_devices`) return typed
:class:`DeviceSnapshot` instances rather than ``dict[str, Any]``; ``client_id``
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
from typing import Dict, List, Optional

from cheshire_drivers.gateway_protocol import DeviceConnectInfo, DeviceStatusInfo
from orca.gateway.registry.snapshot import (
    DeviceSnapshot,
    _ClientState,
    _RegisteredDeviceState,
)

logger = logging.getLogger(__name__)

# One number for "is this client still here": the roster owns it, and
# `connection_source` reports reachability off the same value.
HEARTBEAT_TOLERANCE_SECONDS: float = 30.0


class DeviceNameConflictError(Exception):
    """A client advertised a device another live client already holds.

    One instrument, one connection. Two device bridges claiming the same name
    is a misconfiguration, not a handover: both sockets stay open, both believe
    they own the device, and commands route to whichever registered last. The
    handshake refuses the newcomer rather than letting that happen quietly.
    """

    def __init__(self, conflicts: Dict[str, str]) -> None:
        self.conflicts = conflicts
        detail = ", ".join(
            f"{name} (held by {owner})" for name, owner in sorted(conflicts.items())
        )
        super().__init__(f"device already claimed by another live client: {detail}")



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
      from the next ``ConnectMessage`` on reconnect. orca-core has no
      database dependency.
    """

    def __init__(self) -> None:
        self._clients: Dict[str, _ClientState] = {}
        self._devices: Dict[str, _RegisteredDeviceState] = {}
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

        Raises:
            DeviceNameConflictError: one of the names is already held by a
                different client that is still heartbeating. Nothing is
                registered in that case, so the incumbent keeps its devices.
        """
        async with self._lock:
            now = datetime.now(timezone.utc)

            conflicts = {
                info.name: held.client_id
                for info in devices
                if (held := self._devices.get(info.name)) is not None
                and held.client_id != client_id
                and now - held.snapshot.last_seen
                <= timedelta(seconds=HEARTBEAT_TOLERANCE_SECONDS)
            }
            if conflicts:
                logger.error(
                    "Refusing client %s: %s",
                    client_id, DeviceNameConflictError(conflicts),
                )
                raise DeviceNameConflictError(conflicts)

            self._clients[client_id] = _ClientState(
                site=site,
                lab=lab,
                workcell=workcell,
                connected_at=now,
                last_heartbeat=now,
            )

            for info in devices:
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

    async def unregister_client(
        self,
        client_id: str,
    ) -> None:
        """Unregister a client and mark its devices as offline."""
        async with self._lock:
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

    def peek_interfaces(self, name: str) -> Optional[frozenset[str]]:
        """Synchronously read a device's advertised interface set, or None.

        Sync because the device factory builds drivers inside the synchronous
        ``system.py:build()`` import, where awaiting is not possible. A single
        dict lookup is atomic in CPython, so it needs no lock: the worst-case
        race against a concurrent register/unregister returns either the old
        or the new snapshot, never a torn read, and a stale profile is healed
        on the next rebuild.
        """
        state = self._devices.get(name)
        if state is None:
            return None
        return frozenset(state.snapshot.interfaces)

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

    async def apply_agent_report(self, name: str, reported: DeviceStatusInfo) -> None:
        """Record what the device bridge sees on this device (StatusMessage).

        The device bridge owns the driver objects, so its report is the only
        truthful source for the device's own link state; the control plane's
        own proxy goes stale the moment a command bypasses it.

        The report carries one link per run mode, and this roster answers reads
        that name no mode, so it takes `observed_link` instead of resolving
        one. Resolving would land on the ambient run mode, which is PURE_SIM
        whenever nothing seeded it, and PURE_SIM is not a key the device bridge
        can report. `link_mode` records which driver did answer.
        """
        observed = reported.observed_link
        async with self._lock:
            state = self._devices.get(name)
            if state is not None:
                state.snapshot.status = reported.status
                state.snapshot.link_mode = observed.mode
                state.snapshot.is_connected = observed.is_connected
                state.snapshot.is_initialized = observed.is_initialized
                state.snapshot.last_seen = datetime.now(timezone.utc)

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
        ``last_seen`` against a 30s tolerance. Heartbeats are the
        per-connection liveness signal -- without this per-device
        fan-out, devices whose drivers never push a StatusMessage flip
        to offline 30s after registration and every workflow dispatch
        fails with DeviceOfflineError.
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


# Global device-connection tracker singleton
device_connection_tracker = DeviceConnectionTracker()
