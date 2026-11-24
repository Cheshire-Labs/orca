"""Device connections with no device behind them, for a test runtime that reaches no device."""

from orca.runtime.registry_protocols import IDeviceConnectionSource, IGatewayRegistry
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
    """Empty `IDeviceConnectionSource`: no devices connected, ever."""

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

