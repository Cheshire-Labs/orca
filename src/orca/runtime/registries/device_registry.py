"""DeviceRegistryImpl: composes topology + connection sources into a unified view.

The user mental model is "two cards aligned by name": topology card
(declared) + connection card (reachable now). Either may be present
alone. Connections are the truth;
topology is more like a sim overlay.

This class is composition-only. SRP: it does not own state, does not cache,
and does not persist. `is_client_connected` asks the connection source about
the on-prem client's heartbeat. `is_device_connected` and `is_initialized`
describe one device rather than its client, and come from `DeviceLinkReader`,
which every other read surface shares so two routes cannot answer differently.
The entry carries that read whole, mode included, so its flags cannot come from
two different observations. Topology cards are built fresh on every call from
`ITopologyRegistry.get_device(...)`. `unreachable` names the devices a sim or live run cannot reach.

Wired into `SystemRuntime` as `runtime.devices`. `runtime.topology` and
`runtime.gateway` coexist with this view because their consumers read the
per-surface entry types directly; unifying them is a separate refactor.
"""

from collections.abc import Callable, Iterable

from orca.runtime.registries.device_link import DeviceLinkReader, ResourceLookup
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.registry_protocols import (
    IDeviceConnectionSource,
    IDeviceRegistry,
    ITopologyRegistry,
)
from orca.runtime.errors import (
    TopologyCollisionError,
)
from orca.runtime.status_models import (
    ConnectionCard,
    DeviceRegistryEntry,
    TopologyCard,
    TopologyDeviceEntry,
)

NOT_CONNECTED = "its orca-client is not connected"


class DeviceRegistryImpl(IDeviceRegistry):
    """Two-card composition over topology + connection sources.

    Construction is cheap; every public method walks the underlying sources
    on demand. The registry threads itself through each `DeviceRegistryEntry`
    so `entry.is_client_connected()` can route back to the connection source
    without holding its own pointer.
    """

    def __init__(
        self,
        topology_source: ITopologyRegistry,
        connection_source: IDeviceConnectionSource,
        system: ResourceLookup,
        base_mode: Callable[[], WorkflowRunMode],
    ) -> None:
        self._topology = topology_source
        self._connections = connection_source
        self._links = DeviceLinkReader(connection_source, system, base_mode)

    async def get(self, name: str) -> DeviceRegistryEntry | None:
        topology_entry = self._topology.get_device(name)
        connection_card = await self._connections.get_connection_card(name)
        if topology_entry is None and connection_card is None:
            return None
        topology_card = (
            declared_card(topology_entry)
            if topology_entry is not None
            else None
        )
        mismatch = _interface_mismatch(topology_card, connection_card)
        if mismatch is not None:
            raise TopologyCollisionError(name, mismatch)
        return DeviceRegistryEntry(
            name=name,
            topology_card=topology_card,
            connection_card=connection_card,
            registry=self,
            device_link=await self._links.read(name),
        )

    async def list_all(self) -> list[DeviceRegistryEntry]:
        topology_entries = self._topology.list_devices()
        connection_cards = await self._connections.list_connection_cards()
        topology_by_name: dict[str, TopologyDeviceEntry] = {
            entry.name: entry for entry in topology_entries
        }
        connection_by_name: dict[str, ConnectionCard] = {
            card.name: card for card in connection_cards
        }
        # Stable ordering: topology first (deployment-author intent), then any
        # connection-only stragglers that aren't in topology.
        ordered_names: list[str] = []
        seen: set[str] = set()
        for entry in topology_entries:
            if entry.name not in seen:
                ordered_names.append(entry.name)
                seen.add(entry.name)
        for card in connection_cards:
            if card.name not in seen:
                ordered_names.append(card.name)
                seen.add(card.name)

        result: list[DeviceRegistryEntry] = []
        for name in ordered_names:
            topology_entry = topology_by_name.get(name)
            connection_card = connection_by_name.get(name)
            topology_card = (
                declared_card(topology_entry)
                if topology_entry is not None
                else None
            )
            # One mis-advertised device answers its own row with the mismatch; the rest still list.
            result.append(
                DeviceRegistryEntry(
                    name=name,
                    topology_card=topology_card,
                    connection_card=connection_card,
                    registry=self,
                    device_link=await self._links.read(name),
                    interface_mismatch=_interface_mismatch(topology_card, connection_card),
                ),
            )
        return result

    async def unreachable(self, names: Iterable[str]) -> dict[str, str]:
        """Each named device a sim or live command cannot reach, and why: no declaration, or no connected orca-client."""
        out: dict[str, str] = {}
        for name in names:
            if not self._links.needs_a_client(name):
                continue
            entry = await self.get(name)
            if (
                entry is not None and entry.topology_card is not None and entry.connection_card is not None
                and await entry.is_client_connected()
            ):
                continue
            out[name] = NOT_CONNECTED
        undeclared = await self._undeclared_connected_names() if out else []
        if undeclared:
            hint = f"; connected but not declared in the topology: {', '.join(undeclared)}, possibly a name mismatch"
            out = {name: why + hint for name, why in out.items()}
        return out

    async def _undeclared_connected_names(self) -> list[str]:
        """Live connection names that are not declared in topology.

        Surfaced on a not-connected failure as a name-mismatch hint: a device
        connected under a name the topology does not declare is the usual
        reason a declared device shows as not connected. Filtered by
        `is_connected` (same liveness rule as `unreachable`) so a stale
        registration is not reported as a live connection.
        """
        cards = await self._connections.list_connection_cards()
        names: list[str] = []
        for card in cards:
            if self._topology.get_device(card.name) is not None:
                continue
            if not await self._connections.is_connected(card.name):
                continue
            names.append(card.name)
        return names

    async def _is_client_connected(self, name: str) -> bool:
        return await self._connections.is_connected(name)


def _interface_mismatch(topology_card: TopologyCard | None, connection_card: ConnectionCard | None) -> str | None:
    """Why the connection's interfaces are not a superset of the topology's, or None when they are (C2).

    The kind label is informational. A class default is not a declaration, so it sets no contract.
    """
    if topology_card is None or connection_card is None or topology_card.declared_interfaces_are_class_defaults:
        return None
    missing = sorted(topology_card.declared_interfaces - connection_card.advertised_interfaces)
    if not missing:
        return None
    return (
        f"Device {topology_card.name!r}: connection-advertised "
        f"interfaces {sorted(connection_card.advertised_interfaces)!r} "
        f"is not a superset of topology-declared "
        f"{sorted(topology_card.declared_interfaces)!r}; missing {missing!r}"
    )


def declared_card(entry: TopologyDeviceEntry) -> TopologyCard:
    """What the topology declares for one device."""
    return TopologyCard(
        name=entry.name,
        declared_kind=entry.kind,
        declared_interfaces=frozenset(entry.interfaces),
        mounting_locations=entry.position_ids,
        declared_interfaces_are_class_defaults=entry.interfaces_are_class_defaults,
    )
