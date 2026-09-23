"""Source-available default empty implementations for the device connection surfaces.

`NullGatewayRegistry` covers `IGatewayRegistry` exposed as
`runtime.gateway`. `NullDeviceConnectionSource` covers the unified
DeviceRegistry's connection-card source. Both report "no devices connected"
so a local orca-core run with no gateway gets a well-typed empty view
rather than dereferencing None.

A hosted deployment injects DB-backed implementations in place of these; nothing in
orca-core ever imports those hosted impls.
"""

from orca.runtime.runtime_interface import IDeviceConnectionSource, IGatewayRegistry
from orca.runtime.status_models import (
    ConnectionCard,
    GatewayDeviceEntry,
    ReportedDeviceLink,
)


class NullGatewayRegistry(IGatewayRegistry):
    """Empty `IGatewayRegistry`: no devices connected, ever."""

    async def list_connected(self) -> list[GatewayDeviceEntry]:
        return []

    async def get_gateway_status(self, name: str) -> GatewayDeviceEntry | None:
        del name
        return None


class NullDeviceConnectionSource(IDeviceConnectionSource):
    """Empty `IDeviceConnectionSource`: no devices connected, ever.

    Source-available default for the unified DeviceRegistry's connection card source.
    A hosted deployment injects `DeviceConnectionSource` (which wraps the in-memory
    `DeviceConnectionTracker` plus the persisted `RegisteredDevice` row);
    nothing in orca-core ever imports that hosted impl.
    """

    async def get_connection_card(self, name: str) -> ConnectionCard | None:
        del name
        return None

    async def list_connection_cards(self) -> list[ConnectionCard]:
        return []

    async def is_connected(self, name: str) -> bool:
        del name
        return False

    def peek_reported_link(self, name: str) -> ReportedDeviceLink | None:
        del name
        return None
