"""Typed Pydantic snapshot of a registered device.

The snapshot is the public read shape returned by
:meth:`DeviceConnectionTracker.get_device` and
:meth:`DeviceConnectionTracker.list_devices`.
It replaces the loose ``dict[str, Any]`` that those methods previously
returned, so REST handlers, MCP tool bodies, the controller, and the
gateway service can all use attribute access (``device.type``)
instead of the typo-prone ``device["type"]`` / ``device.get(...)``
shape.

Internal-only state (``client_id``) is intentionally NOT on the snapshot.
The registry tracks ``name -> client_id`` in a parallel map so the
public snapshot never accidentally leaks the client id over the wire
when a handler returns it directly (``GET /api/devices/{name}`` returns
the snapshot as the response body).

The internal :class:`_RegisteredDeviceState` wraps a snapshot with the
client id, which is not part of the public read shape. The registry mutates
the snapshot's ``status`` / link / ``last_seen`` fields in place as frames
land, so the model validates on assignment.
"""

from datetime import datetime

from cheshire_drivers.driver_introspection import MethodInfo
from cheshire_drivers.gateway_protocol import DriverMode

from orca.gateway.device_fault import DeviceFaultSummary
from pydantic import BaseModel, ConfigDict, Field


class DeviceSnapshot(BaseModel):
    """Public read shape for a connected device in the gateway registry.

    Built from :class:`cheshire_drivers.gateway_protocol.DeviceConnectInfo`
    at handshake plus live ``status`` / ``last_seen`` updates from
    StatusMessage and heartbeat traffic. The ``methods`` map preserves
    the typed :class:`MethodInfo` shape rather than its serialized dict
    form so callers can attribute-access ``methods["aspirate"].kind``
    instead of indexing into a nested ``dict[str, Any]``.
    """

    # `validate_assignment` because the registry mutates fields in place as
    # status frames land: without it a wire-shape change writes the wrong type
    # into a field and every reader downstream sees it, silently.
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    type: str
    interfaces: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    provides_state: bool = False
    methods: dict[str, MethodInfo] = Field(default_factory=dict)
    site: str
    lab: str
    workcell: str | None = None
    status: str = "ready"
    # Reads FAULTED_STATUS while `fault` is set. The registry stores the
    # device bridge's own word here; the fault is folded in at the read,
    # because the device bridge has no way to know about it.
    # What the device bridge last reported about the one driver worth showing
    # a reader
    # who is not dispatching. `link_mode` names which of the device's drivers
    # answered; without it a simulator reads as the instrument, or the
    # instrument reads as the simulator the commands are going to. None with
    # both flags False until the first report, which follows the handshake.
    link_mode: DriverMode | None = None
    is_connected: bool = False
    is_initialized: bool = False
    last_seen: datetime
    fault: DeviceFaultSummary | None = None
    """The command that left this device part-way through something, or None.
    The workflow cannot drive the device while it is set. Filled in by the
    read, never by the registry: the fault is the control plane's, not the
    device bridge's."""


class _RegisteredDeviceState:
    """Mutable internal cell holding a snapshot plus the owning client id.

    Kept as a plain dataclass-style object (not a Pydantic model) because it
    is internal bookkeeping, not a wire shape. ``client_id`` lives outside the
    snapshot so the public read shape can never leak it.
    """

    __slots__ = ("client_id", "snapshot")

    def __init__(self, client_id: str, snapshot: DeviceSnapshot) -> None:
        self.client_id = client_id
        self.snapshot = snapshot


class _ClientState:
    """Mutable internal cell for a connected client's session metadata.

    Replaces the loose ``dict[str, Any]`` the tracker kept per client.
    Internal-only: never serialized to the wire.
    """

    __slots__ = ("site", "lab", "workcell", "connected_at", "last_heartbeat")

    def __init__(
        self,
        site: str,
        lab: str,
        workcell: str | None,
        connected_at: datetime,
        last_heartbeat: datetime,
    ) -> None:
        self.site = site
        self.lab = lab
        self.workcell = workcell
        self.connected_at = connected_at
        self.last_heartbeat = last_heartbeat
