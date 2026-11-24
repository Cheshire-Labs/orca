"""Registry protocols: the read surfaces over topology, gateway connections, faults and devices."""

from collections.abc import Iterable
from typing import Protocol

from orca.gateway.device_fault import DeviceFault
from orca.runtime.status_models import (
    ConnectionCard,
    DeviceRegistryEntry,
    GatewayDeviceEntry,
    ReportedDeviceLink,
    TopologyDeviceEntry,
)


# -- ITopologyRegistry / IGatewayRegistry -----------------------------------


class ITopologyRegistry(Protocol):
    """Read-only view of devices declared in the deployment topology.

    Backed by the System graph: each `Transporter(...)`, `Shaker(...)`,
    `LiquidHandler(...)` etc. registers a Resource on
    `resource_models.resource_registry`. The source-available implementation
    (`SystemTopologyRegistry`) walks that registry on demand. This Protocol
    is what `runtime.topology` exposes; consumers (DeviceFacade, the
    collision validator, a hosted REST surface) program against it instead of
    reaching into the System directly.
    """

    def list_devices(self) -> list[TopologyDeviceEntry]: ...
    def get_device(self, name: str) -> TopologyDeviceEntry | None: ...


class IGatewayRegistry(Protocol):
    """Read-only view of devices reachable through the device bridge.

    Both servers pass `DeviceConnectionSource`, which reads the connections `/ws/devices` records.
    """

    async def list_connected(self) -> list[GatewayDeviceEntry]: ...
    async def get_gateway_status(self, name: str) -> GatewayDeviceEntry | None: ...


class IDeviceFaultSource(Protocol):
    """Which devices were left part-way through a command nobody has checked.

    The device gateway latches a fault when a command fails, times out, or is
    cancelled after it went out on the wire, and keeps it until an operator
    clears it. Read so device status can report it; cleared so the workflow can
    drive the device again.
    """

    def fault(self, device_id: str) -> DeviceFault | None: ...
    async def clear_fault(self, device_id: str) -> DeviceFault | None: ...
    def bring_up_error(self, device_id: str) -> str | None:
        """Why a switch between sim and live could not bring the device up, or None."""
        ...


# -- Unified DeviceRegistry --------------------------------------------------
# Composes topology + connection sources into a single read surface keyed
# by device name. Live state (is_connected, is_initialized) is queried on
# demand against the connection source or driver, never cached or persisted
# on registry fields.


class IDeviceConnectionSource(Protocol):
    """Source of connection-card information for the unified DeviceRegistry.

    Both servers pass `DeviceConnectionSource`, which reads the connections `/ws/devices` records.

    `is_connected` answers live; the registry never caches the result.
    `last_heartbeat` semantics (and the staleness threshold) are owned by
    the source implementation.

    A card also carries `device_is_connected` / `device_is_initialized` and the
    `device_link_mode` that names which driver answered: what the on-prem
    device bridge last reported about that device's own driver. The device
    bridge holds the driver, so it is the only party that can answer those. A
    source reporting nothing for a device does NOT mean "read the local driver
    instead": that is only true where no device bridge holds the device at all,
    and `DeviceLinkReader` owns the distinction. See its module docstring for
    the whole rule.

    `peek_reported_link` is the same answer without the rest of the card, and
    synchronous so the runtime's synchronous snapshot builders can source their
    flags from here instead of from a proxy's cache.
    """

    async def get_connection_card(self, name: str) -> ConnectionCard | None: ...

    def peek_reported_link(self, name: str) -> ReportedDeviceLink | None: ...
    async def list_connection_cards(self) -> list[ConnectionCard]: ...
    async def is_connected(self, name: str) -> bool: ...


class IDeviceRegistry(Protocol):
    """Unified read surface for device registry entries.

    Composes the topology (declared) and connection (reachable now) cards
    into one two-card view. Either card may be present alone; absence is
    the signal (no `unknown_to_topology` flag). The user mental model is: connections are the truth, topology is
    the declaration overlay.

    Live state (`is_connected`, `is_initialized`) is queried per call on
    `DeviceRegistryEntry`. The registry holds no state beyond pointers to
    the topology + connection sources.

    `unreachable` names the devices a sim or live run cannot reach. `DeviceController` validates against
    `effective_interfaces`. This Protocol is the integration point.
    """

    async def get(self, name: str) -> DeviceRegistryEntry | None: ...
    async def list_all(self) -> list[DeviceRegistryEntry]: ...
    async def unreachable(self, names: Iterable[str]) -> dict[str, str]: ...
