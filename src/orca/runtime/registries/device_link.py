"""One answer to "is this device linked, and is it brought up".

Every read surface that reports a device's own link goes through here: the
unified device registry, and the snapshot builders behind `GET /api/devices/{name}`
and `orca device show`. Sourcing them separately is how two adjacent routes
came to print contradicting flags for the same device at the same instant.

The rule is "answer for the driver that is actually being driven, and say which
one that was":

1. **A device bridge reported.** It holds the driver objects, so its report
   wins, and the mode it names comes with it. The device bridge reports one
   link per run mode and collapses them itself, so nothing here resolves a mode.
2. **No report, and a device bridge holds this device.** Whatever orca has in
   process is not an answer about the instrument: it is a proxy holding a cache
   of the last command through it. So the answer is unknown. Reported as closed with
   no mode: a boolean field has nowhere else to put "nobody here can answer",
   and `is_client_connected` next to it says the device bridge is gone.
3. **No device bridge anywhere.** orca holds the driver, so the read reports the
   dispatch driver, the same one `connect` / `initialize` / `disconnect` act on
   (`Device.driver`). An operator who just ran `initialize` has to see the
   effect of what they ran, so this resolves the same base those verbs do (an
   active run's mode, else live), and `mode` says which world that was. That
   label is the whole mitigation.
   Reading the live driver instead would describe a driver those verbs never
   touched, so `connect` would return 200 and move nothing a reader can see.

Not every reader comes through here. `Transporter.ensure_initialized` reads its
proxy's cached flag directly before every pick, because that read is
synchronous and cannot await a wire query. `forget_driver_session` is what
keeps that cache honest: it expires when the device bridge session behind it
does.
"""

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from cheshire_drivers.gateway_protocol import EffectiveMode

from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resources import IResource
from orca.driver_management.device_factory_protocol import DeviceDriver
from orca.runtime.registry_protocols import IDeviceConnectionSource
from orca.runtime.status_models import ReportedDeviceLink


_UNKNOWN = ReportedDeviceLink(mode=None, is_connected=False, is_initialized=False)


class ResourceLookup(Protocol):
    """The two calls a link read needs from the system's resource registry."""

    def has_resource(self, name: str) -> bool: ...
    def get_resource(self, name: str) -> IResource: ...


class DriverLink(Protocol):
    """A driver reporting on its own link to the instrument."""

    @property
    def is_connected(self) -> bool: ...

    @property
    def is_initialized(self) -> bool: ...


# The attribute a driver carries once a factory has stamped it. Named here,
# next to the only thing that reads it, so the stamp and the test move together.
AGENT_HELD_ATTRIBUTE = "instrument_is_held_remotely"


@runtime_checkable
class _AgentHeld(Protocol):
    """A driver whose instrument is reached through an on-prem device bridge."""

    @property
    def instrument_is_held_remotely(self) -> bool: ...


def is_agent_held(driver: DeviceDriver) -> bool:
    """Whether a device bridge holds this driver's instrument."""
    return isinstance(driver, _AgentHeld)


def mark_agent_held(driver: DeviceDriver) -> DeviceDriver:
    """Record that this driver reaches its instrument through a device bridge; stamped by the factory that builds it."""
    setattr(driver, AGENT_HELD_ATTRIBUTE, True)
    return driver


@runtime_checkable
class _SessionCaching(Protocol):
    """A driver holding link flags that only its device bridge's live session
    justifies."""

    def forget_driver_session(self) -> None: ...


def forget_driver_session(driver: DeviceDriver) -> None:
    """Expire a driver's cached link flags, if it keeps any.

    The push half of the rules above: a device bridge that went away or came
    back invalidates what its proxy cached, so a synchronous `is_initialized`
    read never has to reach for the wire to stay honest. A local simulator in a
    live slot caches nothing across a session and is skipped.
    """
    if isinstance(driver, _SessionCaching):
        driver.forget_driver_session()


@runtime_checkable
class _DrivenResource(Protocol):
    """A resource whose lifecycle verbs dispatch through its driver."""

    @property
    def driver(self) -> DriverLink: ...

    async def mode_under(self, base: WorkflowRunMode) -> EffectiveMode: ...


class DeviceLinkReader:
    """Reads a device's link from whoever is actually driving it."""

    def __init__(
        self, connections: IDeviceConnectionSource, system: ResourceLookup,
        base_mode: Callable[[], WorkflowRunMode],
    ) -> None:
        self._connections = connections
        self._system = system
        self._base_mode = base_mode

    async def read(self, name: str) -> ReportedDeviceLink:
        reported = self._connections.peek_reported_link(name)
        if reported is not None:
            return reported
        return await self._local_read(name)

    async def is_initialized(self, name: str) -> bool:
        return (await self.read(name)).is_initialized

    def needs_a_client(self, name: str) -> bool:
        """Whether the device's driver reaches it through an orca-client; a test's in-process driver does not."""
        if not self._system.has_resource(name):
            return True
        resource = self._system.get_resource(name)
        return not isinstance(resource, _DrivenResource) or isinstance(resource.driver, _AgentHeld)

    async def _local_read(self, name: str) -> ReportedDeviceLink:
        if not self._system.has_resource(name):
            return _UNKNOWN
        resource = self._system.get_resource(name)
        if not isinstance(resource, _DrivenResource):
            return _UNKNOWN
        # Whether a device bridge holds this device, not what its driver
        # currently says: if one does, its silence is the whole answer and
        # whatever orca kept in process is not a substitute.
        driver = resource.driver
        if isinstance(driver, _AgentHeld):
            return _UNKNOWN
        return ReportedDeviceLink(
            mode=await resource.mode_under(self._base_mode()),
            is_connected=driver.is_connected,
            is_initialized=driver.is_initialized,
        )
