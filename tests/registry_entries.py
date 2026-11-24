"""Real device registry entries for tests of the interface rule."""

from unittest.mock import AsyncMock, MagicMock

from orca.runtime.status_models import ConnectionCard, DeviceRegistryEntry, ReportedDeviceLink, TopologyCard


def registry_entry(
    declared: frozenset[str] | None, advertised: frozenset[str] | None, *, class_defaults: bool = False,
) -> DeviceRegistryEntry:
    """A real registry entry with the cards given; None leaves that card out."""
    topology = None if declared is None else TopologyCard(
        name="dev_1", declared_kind="shaker", declared_interfaces=declared, mounting_locations=(),
        declared_interfaces_are_class_defaults=class_defaults,
    )
    connection = None if advertised is None else ConnectionCard(
        name="dev_1", client_id="lab_pc", connection_id="c1", last_heartbeat=None, advertised_kind="shaker",
        advertised_interfaces=advertised,
    )
    link = ReportedDeviceLink(mode=None, is_connected=False, is_initialized=False)
    return DeviceRegistryEntry("dev_1", topology, connection, MagicMock(), link)


def registry_holding(entry: DeviceRegistryEntry | None) -> MagicMock:
    registry = MagicMock()
    registry.get = AsyncMock(return_value=entry)
    return registry
