"""The wire shapes of a device's setup file, importable without the engine."""

from cheshire_drivers.device_setup import DeviceSetup
from pydantic import BaseModel, ConfigDict
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag

_RELOAD_NOTE = "Takes effect on the next reload; the running system keeps the setup it was built from until then."


class GetDeviceSetupRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str


class PutDeviceSetupRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    setup: DeviceSetup
    reason: str | None = None


class DeviceSetupResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_name: str
    setup: DeviceSetup
    reload_note: str = _RELOAD_NOTE


GET_DEVICE_SETUP_ROUTE = Route(
    GetDeviceSetupRequest,
    DeviceSetupResponse,
    RestBinding(
        method="GET", path="/devices/{device_id}/setup", tag=RouteTag.DEVICES,
    ),
)


PUT_DEVICE_SETUP_ROUTE = Route(
    PutDeviceSetupRequest,
    DeviceSetupResponse,
    RestBinding(
        method="PUT", path="/devices/{device_id}/setup", tag=RouteTag.DEVICES,
    ),
)
