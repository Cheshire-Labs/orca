"""Device registry wire models and routes."""

from pydantic import BaseModel, ConfigDict
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.wire_models import DeviceRegistryEntryResponse, DeviceRegistryListResponse


class ListDeviceRegistryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ShowDeviceRegistryEntryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str


LIST_DEVICE_REGISTRY_ROUTE = Route(
    ListDeviceRegistryRequest,
    DeviceRegistryListResponse,
    RestBinding(
        method="GET", path="/devices", tag=RouteTag.DEVICES,
    ),
)


SHOW_DEVICE_REGISTRY_ENTRY_ROUTE = Route(
    ShowDeviceRegistryEntryRequest,
    DeviceRegistryEntryResponse,
    RestBinding(
        method="GET", path="/devices/{device_id}", tag=RouteTag.DEVICES,
    ),
)
