"""The device-bridge gateway parts a server builds once at startup (orca.runtime.startup) and passes to everything that needs them."""

from dataclasses import dataclass

from orca.gateway.controller.controller import IDeviceController
from orca.gateway.registry.connection_tracker import IDeviceConnectionTracker
from orca.gateway.websocket.assignments import IDeviceAssignments
from orca.gateway.websocket.connection_events import IConnectionEvents
from orca.gateway.websocket.manager import IClientChannel, IConnectionManager


@dataclass(frozen=True)
class DeviceGateway:
    """The connected-device roster, the channel commands go out on, their connection signals, the command controller, what each client runs, and what each client said its devices are."""

    tracker: IDeviceConnectionTracker
    connections: IClientChannel
    events: IConnectionEvents
    controller: IDeviceController
    assignments: IDeviceAssignments


@dataclass(frozen=True)
class ServerDeviceGateway(DeviceGateway):
    """A server's gateway, which also holds the sockets its `/ws/devices` route accepts.

    `build_device_gateway` makes `connections` and `sockets` one `ConnectionManager`, so a command goes out on the
    socket the route accepted.
    """

    sockets: IConnectionManager
