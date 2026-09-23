"""DeviceConnectionSource: orca-core IDeviceConnectionSource backed by gateway state.

The device-integration gateway tracks device state in a hot, in-memory
layer: `DeviceConnectionTracker` keyed by name (the operator-visible
identifier carried on `DeviceConnectInfo.name` and matched against the
topology declaration `Shaker(name=...)`), populated when the device bridge
opens its WebSocket and advertises its devices. It carries `last_seen`,
`status`, the observed kind label, and the interface advertisement. This is
the source of truth for "is the device reachable right now."

`DeviceConnectionSource` implements both contracts the unified DeviceRegistry
needs:

- `IDeviceConnectionSource`: the connection-card source feeding
  `runtime.device_registry`. Builds one `ConnectionCard` per reachable
  device.
- `IGatewayRegistry`: backs `runtime.gateway`, building one
  `GatewayDeviceEntry` per reachable device.

Both surfaces share the underlying `DeviceConnectionTracker` so a single
adapter satisfies both. The hosting layer constructs one instance of this
class and passes it twice to `RuntimeLifecycle` / `SystemRuntime` (once as
`gateway_registry`, once as `connection_source`). The orca-core collision
validator and the DeviceFacade union view consume `IGatewayRegistry`; the
unified registry consumes `IDeviceConnectionSource`.
"""

from datetime import datetime, timezone

from orca.runtime.runtime_interface import IDeviceConnectionSource, IGatewayRegistry
from orca.runtime.status_models import (
    ConnectionCard,
    GatewayDeviceEntry,
    ReportedDeviceLink,
)

from orca.gateway.registry.connection_tracker import (
    HEARTBEAT_TOLERANCE_SECONDS as _HEARTBEAT_TOLERANCE_SECONDS,
    DeviceConnectionTracker,
)
from orca.gateway.registry.snapshot import DeviceSnapshot

# Connections older than this count as disconnected. Chosen to accommodate
# the typical device-bridge heartbeat cadence (10s nominal) plus burst-loss
# tolerance; a longer pause is the disconnect-policy's job, not the
# registry's. The unified registry queries this source on every dispatch,
# so the threshold also bounds the worst-case "stale view" window.



def _entry_from_snapshot(snapshot: DeviceSnapshot) -> GatewayDeviceEntry:
    """Adapt a typed :class:`DeviceSnapshot` to a GatewayDeviceEntry."""
    return GatewayDeviceEntry(
        name=snapshot.name,
        driver_class_observed=snapshot.type,
        interfaces=tuple(snapshot.interfaces),
        last_heartbeat=snapshot.last_seen,
        connection_id=None,  # client_id is internal; not part of the public entry.
        status=snapshot.status,
    )


def _connection_card_from_snapshot(
    snapshot: DeviceSnapshot, client_id: str,
) -> ConnectionCard:
    """Adapt a typed :class:`DeviceSnapshot` to a ConnectionCard."""
    return ConnectionCard(
        name=snapshot.name,
        client_id=client_id,
        # Connection-id is not currently surfaced by the tracker; the
        # client_id is the closest stable identity per WebSocket session.
        # When the device bridge starts emitting per-session connection_ids,
        # plumb them through here.
        connection_id=client_id,
        last_heartbeat=snapshot.last_seen,
        advertised_kind=snapshot.type,
        advertised_interfaces=frozenset(snapshot.interfaces),
        device_is_connected=snapshot.is_connected,
        device_is_initialized=snapshot.is_initialized,
        device_link_mode=snapshot.link_mode,
    )


class DeviceConnectionSource(IDeviceConnectionSource, IGatewayRegistry):
    """Adapter exposing gateway state via both registry contracts.

    Implements `IGatewayRegistry` (`runtime.gateway`) and
    `IDeviceConnectionSource` (`runtime.device_registry`'s connection card
    source). One instance serves both; the hosting layer wires the same
    object as both `gateway_registry` and `connection_source` on the runtime.
    """

    def __init__(self, registry: DeviceConnectionTracker) -> None:
        self._registry = registry

    # -- IGatewayRegistry surface -------------------------------------------

    async def list_connected(self) -> list[GatewayDeviceEntry]:
        snapshots = await self._registry.list_devices()
        return [_entry_from_snapshot(snap) for snap in snapshots]

    async def get_gateway_status(self, name: str) -> GatewayDeviceEntry | None:
        # The DeviceConnectionTracker indexes by name (the operator-visible
        # identifier carried on `DeviceConnectInfo.name` and matched against
        # the topology declaration `Shaker(name=...)`).
        snapshot = await self._registry.get_device(name)
        if snapshot is None:
            return None
        return _entry_from_snapshot(snapshot)

    # -- IDeviceConnectionSource surface ------------------------------------

    async def get_connection_card(self, name: str) -> ConnectionCard | None:
        # Single atomic lookup. The previous two-call pattern
        # (get_device then get_client_for_device) raced with
        # unregister_client and required fabricating client_id="" for a
        # device the caller had already decided existed -- a wire-shape
        # lie. The atomic accessor returns None when the device row
        # vanishes (which is what "no card" means anyway) and a real
        # (snapshot, client_id) pair otherwise.
        result = await self._registry.get_device_with_client(name)
        if result is None:
            return None
        snapshot, client_id = result
        return _connection_card_from_snapshot(snapshot, client_id)

    async def list_connection_cards(self) -> list[ConnectionCard]:
        return [
            _connection_card_from_snapshot(snapshot, client_id)
            for snapshot, client_id in await self._registry.list_devices_with_clients()
        ]

    async def is_connected(self, name: str) -> bool:
        snapshot = await self._registry.get_device(name)
        if snapshot is None:
            return False
        delta = datetime.now(timezone.utc) - snapshot.last_seen
        return delta.total_seconds() <= _HEARTBEAT_TOLERANCE_SECONDS

    def peek_reported_link(self, name: str) -> ReportedDeviceLink | None:
        snapshot = self._registry.peek_snapshot(name)
        if snapshot is None:
            return None
        return ReportedDeviceLink(
            mode=snapshot.link_mode,
            is_connected=snapshot.is_connected,
            is_initialized=snapshot.is_initialized,
        )
