"""One answer to "is this device linked, and is it brought up".

Every read surface that reports a device's own link goes through here: the
unified device registry, and the snapshot builders behind `GET /devices/{name}`
and `orca device show`. Sourcing them separately is how two adjacent routes
came to print contradicting flags for the same device at the same instant.

The rule is "answer for the driver that is actually being driven, and say which
one that was":

1. **A device bridge reported.** It holds the driver objects, so its report
   wins, and the mode it names comes with it. The device bridge reports one
   link per run mode and collapses them itself; nothing here resolves a mode,
   because resolving one on a read path yields PURE_SIM whenever nothing seeded
   it and that is not a mode a device bridge can report.
2. **No report, and a device bridge holds this device.** Whatever orca has in
   process is not an answer about the instrument: for a device with a wire
   surface it is a proxy holding a cache of the last command through it, and
   for a passive one (storage, waste) it is a local simulator orca's own
   bring-up walk initialized. So the answer is unknown. Reported as closed with
   no mode: a boolean field has nowhere else to put "nobody here can answer",
   and `is_client_connected` next to it says the device bridge is gone.
3. **No device bridge anywhere.** orca holds the driver, so the read reports the
   dispatch driver, the same one `connect` / `initialize` / `disconnect` act on
   (`Device.driver`). An operator who just ran `initialize` has to see the
   effect of what they ran, so this resolves the same `OPERATOR_DEVICE_WRITE_BASE`
   those verbs do, and `mode` says which world that was: LIVE unless the
   topology declares the device sim. That label is the whole mitigation.
   Reading the live driver instead would describe a driver those verbs never
   touched, so `connect` would return 200 and move nothing a reader can see.

Not every reader comes through here. `Transporter.ensure_initialized` reads its
proxy's cached flag directly before every pick, because that read is
synchronous and cannot await a wire query. `forget_driver_session` is what
keeps that cache honest: it expires when the device bridge session behind it
does.
"""

from typing import Protocol, runtime_checkable

from cheshire_drivers.gateway_protocol import EffectiveMode

from orca.runtime.run_modes import (
    OPERATOR_DEVICE_WRITE_BASE,
    WorkflowRunMode,
)
from orca.resource_models.resources import IResource
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.runtime_interface import IDeviceConnectionSource
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


def mark_agent_held(driver: DriverPairElement) -> DriverPairElement:
    """Record that this driver reaches its instrument through a device bridge.

    Stamped by the factory that builds it, because being built there is what
    makes it true. Not declared on the driver classes: a passive device type
    gets a local simulator in its live slot rather than a proxy, so class
    identity does not answer the question and copying a marker onto each class
    would keep missing the ones that are not proxies at all.
    """
    setattr(driver, AGENT_HELD_ATTRIBUTE, True)
    return driver


@runtime_checkable
class _SessionCaching(Protocol):
    """A driver holding link flags that only its device bridge's live session
    justifies."""

    def forget_driver_session(self) -> None: ...


def forget_driver_session(driver: DriverPairElement) -> None:
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
    """A resource whose lifecycle verbs dispatch through a run-mode-picked driver."""

    @property
    def driver(self) -> DriverLink: ...

    @property
    def live_driver(self) -> DriverLink: ...

    @property
    def effective_mode(self) -> EffectiveMode: ...

    def mode_under(self, base: WorkflowRunMode) -> EffectiveMode: ...

    def driver_under(self, base: WorkflowRunMode) -> DriverLink: ...


class DeviceLinkReader:
    """Reads a device's link from whoever is actually driving it."""

    def __init__(
        self, connections: IDeviceConnectionSource, system: ResourceLookup,
    ) -> None:
        self._connections = connections
        self._system = system

    def read(self, name: str) -> ReportedDeviceLink:
        reported = self._connections.peek_reported_link(name)
        if reported is not None:
            return reported
        return self._local_read(name)

    def is_initialized(self, name: str) -> bool:
        return self.read(name).is_initialized

    def _local_read(self, name: str) -> ReportedDeviceLink:
        if not self._system.has_resource(name):
            return _UNKNOWN
        resource = self._system.get_resource(name)
        if not isinstance(resource, _DrivenResource):
            return _UNKNOWN
        # Whether a device bridge holds this device, not what its driver
        # currently says: if one does, its silence is the whole answer and
        # whatever orca kept in process is not a substitute.
        if isinstance(resource.live_driver, _AgentHeld):
            return _UNKNOWN
        # The flags must describe the world operator verbs act in (LIVE by
        # default, ratchet on top), or the verbs move flags no read shows.
        driver = resource.driver_under(OPERATOR_DEVICE_WRITE_BASE)
        return ReportedDeviceLink(
            mode=resource.mode_under(OPERATOR_DEVICE_WRITE_BASE),
            is_connected=driver.is_connected,
            is_initialized=driver.is_initialized,
        )
