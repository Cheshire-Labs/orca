"""SystemTopologyRegistry: walks the System graph to enumerate declared devices.

This is the source-available, in-process implementation of `ITopologyRegistry`. It reads
from `system.resource_registry`: every `Transporter(...)`, `Shaker(...)`,
`LiquidHandler(...)`, etc. registered there counts as a topology declaration.
For each device the registry emits a `TopologyDeviceEntry` carrying the
operator-visible name, the kind label (e.g. ``"shaker"``), the
cheshire-drivers `interfaces` ClassVar (declared capability contract), and
the locations the device is mounted at on the system map.

The kind label comes from the resource subclass's ``KIND`` ClassVar
(``Shaker.KIND == "shaker"``). It matches the ``DeviceConnectInfo.type``
string a connected orca-client advertises so operator-facing surfaces
display the same label on both sides.
"""

from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from orca.resource_models.devices import Device
from orca.resource_models.transporter import Transporter
from orca.runtime.runtime_interface import ITopologyRegistry
from orca.runtime.status_models import TopologyDeviceEntry
from orca.system.system_interface import ISystem


class SystemTopologyRegistry(ITopologyRegistry):
    """`ITopologyRegistry` backed by the live System resource registry."""

    def __init__(self, system: ISystem) -> None:
        self._system = system

    def list_devices(self) -> list[TopologyDeviceEntry]:
        entries: list[TopologyDeviceEntry] = []
        for device in self._system.devices:
            entries.append(self._build_entry(device))
        for transporter in self._system.transporters:
            entries.append(self._build_entry(transporter))
        return entries

    def get_device(self, name: str) -> TopologyDeviceEntry | None:
        if not self._system.has_resource(name):
            return None
        resource = self._system.get_resource(name)
        if not isinstance(resource, (Device, Transporter)):
            return None
        return self._build_entry(resource)

    @staticmethod
    def _build_entry(resource: Device | Transporter) -> TopologyDeviceEntry:
        # Topology cards describe the deployment's declared capability
        # surface, which is the live driver's contract. Reading through
        # `resource.driver` (the dispatch property) would leak the sim
        # driver's interfaces under an unseeded `current_run_mode` because
        # dispatch falls back to PURE_SIM there. Sim and live drivers are
        # not required to declare identical `interfaces` ClassVar sets.
        driver = resource.live_driver
        # A local driver's ClassVar IS its declaration. A gateway-built one's is
        # a placeholder until its client connects and fills the advertised card.
        gateway_backed = isinstance(driver, GatewayBackedDriver)
        declared = driver.declared_interfaces if gateway_backed else None
        interfaces_are_class_defaults = gateway_backed and declared is None
        interfaces_source = (
            declared if declared is not None
            else getattr(type(driver), "interfaces", frozenset())
        )
        interfaces: tuple[str, ...] = tuple(sorted(interfaces_source))
        # Transporters reach named positions via their teachpoint store rather
        # than being mounted at fixed Locations the way Devices are. Empty
        # tuple for transporters reflects "not mounted at a single location."
        if isinstance(resource, Device):
            position_ids: tuple[str, ...] = tuple(
                loc.name for loc in resource.locations
            )
        else:
            position_ids = ()
        return TopologyDeviceEntry(
            name=resource.name,
            kind=type(resource).KIND,
            interfaces=interfaces,
            position_ids=position_ids,
            sim_override=resource.sim_override,
            interfaces_are_class_defaults=interfaces_are_class_defaults,
        )
