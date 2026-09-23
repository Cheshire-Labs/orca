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
`ITopologyRegistry.get_device(...)`. Workflow-mode validation
(`assert_runnable`) iterates every name and consults each entry.

Wired into `SystemRuntime` as `runtime.devices`. `runtime.topology` and
`runtime.gateway` coexist with this view because their consumers read the
per-surface entry types directly; unifying them is a separate refactor.
"""

from collections.abc import Iterable

from orca.runtime.registries.device_link import DeviceLinkReader, ResourceLookup
from orca.runtime.run_modes import resolve_effective_mode_for_device
from orca.runtime.runtime_interface import (
    IDeviceConnectionSource,
    IDeviceRegistry,
    ITopologyRegistry,
    TopologyCollisionError,
    WorkflowDeviceMissingError,
    WorkflowDeviceNotConnectedError,
)
from orca.runtime.status_models import (
    ConnectionCard,
    DeviceRegistryEntry,
    TopologyCard,
    TopologyDeviceEntry,
    WorkflowRunMode,
)


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
    ) -> None:
        self._topology = topology_source
        self._connections = connection_source
        self._links = DeviceLinkReader(connection_source, system)

    async def get(self, name: str) -> DeviceRegistryEntry | None:
        topology_entry = self._topology.get_device(name)
        connection_card = await self._connections.get_connection_card(name)
        if topology_entry is None and connection_card is None:
            return None
        topology_card = (
            self._build_topology_card(topology_entry)
            if topology_entry is not None
            else None
        )
        if topology_card is not None and connection_card is not None:
            self._verify_kind_match(topology_card, connection_card)
        return DeviceRegistryEntry(
            name=name,
            topology_card=topology_card,
            connection_card=connection_card,
            registry=self,
            device_link=self._links.read(name),
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
                self._build_topology_card(topology_entry)
                if topology_entry is not None
                else None
            )
            if topology_card is not None and connection_card is not None:
                self._verify_kind_match(topology_card, connection_card)
            result.append(
                DeviceRegistryEntry(
                    name=name,
                    topology_card=topology_card,
                    connection_card=connection_card,
                    registry=self,
                    device_link=self._links.read(name),
                ),
            )
        return result

    async def assert_runnable(
        self, names: Iterable[str], mode: WorkflowRunMode,
    ) -> None:
        """Validate that every named device can run under `mode` (per R1).

        The check is per-device: a device's topology `sim_override` can ratchet
        it to PURE_SIM under a LIVE / DEVICE_SIM submission, so connection is
        required only when the *resolved* mode hits the wire.

        PURE_SIM-resolved: the device needs a topology card.
        DEVICE_SIM / LIVE-resolved: the device needs both cards AND a live
        connection.

        Aggregates every offending name into a single error rather than
        failing on the first; operators get the full picture in one shot.
        """
        missing_topology: list[str] = []
        missing_connection: list[str] = []
        for name in names:
            entry = await self.get(name)
            if entry is None or entry.topology_card is None:
                # Undeclared in topology: no override exists, so the submission
                # mode stands. PURE_SIM needs the declaration; wire modes report
                # it as not-connected.
                if mode is WorkflowRunMode.PURE_SIM:
                    missing_topology.append(name)
                else:
                    missing_connection.append(name)
                continue
            resolved = resolve_effective_mode_for_device(
                mode, entry.topology_sim_override(),
            ).resolved
            if resolved is WorkflowRunMode.PURE_SIM:
                continue
            if entry.connection_card is None or not await entry.is_client_connected():
                missing_connection.append(name)
        if missing_topology:
            raise WorkflowDeviceMissingError(missing_topology)
        if missing_connection:
            raise WorkflowDeviceNotConnectedError(
                missing_connection,
                undeclared_connected=await self._undeclared_connected_names(),
            )

    async def _undeclared_connected_names(self) -> list[str]:
        """Live connection names that are not declared in topology.

        Surfaced on a not-connected failure as a name-mismatch hint: a device
        connected under a name the topology does not declare is the usual
        reason a declared device shows as not connected. Filtered by
        `is_connected` (same liveness rule as `assert_runnable`) so a stale
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

    @staticmethod
    def _build_topology_card(entry: TopologyDeviceEntry) -> TopologyCard:
        # `sim_override` flows from `Device(sim_override=...)` /
        # `Transporter(sim_override=...)` through the topology registry to
        # `TopologyCard.topology_sim_override`, which the mode resolver
        # reads as the "topology per-device" precedence layer.
        return TopologyCard(
            name=entry.name,
            declared_kind=entry.kind,
            declared_interfaces=frozenset(entry.interfaces),
            mounting_locations=entry.position_ids,
            topology_sim_override=entry.sim_override,
            disconnect_timeout_seconds=None,
            declared_interfaces_are_class_defaults=entry.interfaces_are_class_defaults,
        )

    @staticmethod
    def _verify_kind_match(
        topology_card: TopologyCard, connection_card: ConnectionCard,
    ) -> None:
        """Per C2: connection's advertised interfaces must be a superset of topology's.

        Kind label string is informational, not enforced. A connection
        advertising a strict superset (e.g. IShaker + ITempSettable when
        topology declares IShaker) passes; a connection advertising a
        non-overlapping or incomplete set (e.g. IDelidder vs IShaker) raises
        `TopologyCollisionError` so the runtime fails loud rather than
        dispatching a workflow against a contract the wire can't honor.
        """
        if topology_card.declared_interfaces_are_class_defaults:
            # A class default is not a declaration, so there is no contract to
            # be a superset of.
            return
        if topology_card.declared_interfaces.issubset(
            connection_card.advertised_interfaces,
        ):
            return
        missing = sorted(
            topology_card.declared_interfaces
            - connection_card.advertised_interfaces,
        )
        raise TopologyCollisionError(
            f"Device {topology_card.name!r}: connection-advertised "
            f"interfaces {sorted(connection_card.advertised_interfaces)!r} "
            f"is not a superset of topology-declared "
            f"{sorted(topology_card.declared_interfaces)!r}; missing {missing!r}",
        )
