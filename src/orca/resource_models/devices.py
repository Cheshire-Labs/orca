from abc import ABC
from contextlib import asynccontextmanager
import logging
from cheshire_drivers.command_timings import CommandTiming, collect_command_timings
from cheshire_drivers.device_sites import DEFAULT_SITE
from cheshire_drivers.interfaces import BaseDriver
from orca.modes.device_modes import DeviceMode, IDeviceModes
from orca.resource_models.resources import EngineCommandLog, IDevice, IModeAware, engine_time_limit
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.location import Location
from typing import AsyncIterator, ClassVar, Generic, List, TypeVar

from orca.resource_models.device_run_mode import DeviceRunMode
from orca.resource_models.tracked_lock import TrackedLock
from orca.modes.run_modes import BaseMode, WorkflowRunMode, out_of_run

orca_logger = logging.getLogger("orca")



TDriver = TypeVar('TDriver', bound=BaseDriver)
class Device(IDevice, IModeAware, Generic[TDriver], ABC):
    """A processing engine that operates on labware.

    Does NOT hold labware directly. Labware sits on LabwareStagingBridge
    (the Location's resource). Device provides the lock, driver,
    and hardware hook methods (_do_*).

    Subclasses MUST declare a ``KIND`` class variable (e.g. ``"shaker"``,
    ``"liquid_handler"``). The label flows through the topology registry
    onto ``TopologyCard.declared_kind`` and is the same string a connected
    orca-client advertises as ``DeviceConnectInfo.type``. The label is
    advisory at the contract level (interfaces are the safety gate); it
    drives dispatch defaults like per-kind timeouts and labels devices in
    operator-facing surfaces.
    """

    KIND: ClassVar[str]

    @property
    def deck_modeling(self) -> bool:
        """True for a liquid handler that keeps a model of its deck."""
        return False

    def __init__(self,
                 name: str,
                 driver: TDriver,
                 site_names: list[str] | None = None,
                 ) -> None:
        self._name = name
        # Physical labware positions this device owns: one site by
        # default; multi-position devices (centrifuge buckets) declare theirs.
        self._site_names: list[str] = list(site_names) if site_names else [DEFAULT_SITE]
        self._driver = driver
        self._run_mode = DeviceRunMode(name)
        self._engine_commands = EngineCommandLog(name, self._engine_time_limit)
        self._is_initialized = False
        self._lock = TrackedLock(f"{name} device lock")
        self._locations: List[Location] = []
        self._sites: List[Location] = []
        # A count, not a flag: a refused operator command closes its scope while another is still open.
        self._external_control_scopes = 0
        self._external_control_hold: str | None = None
        self._offline = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def driver(self) -> TDriver:
        return self._driver

    @property
    def lock(self) -> TrackedLock:
        return self._lock

    @asynccontextmanager
    async def reach_in_lease(self, mover: IPlateMover, purpose: str) -> AsyncIterator[None]:
        """Hold this device while ``mover`` reaches into it."""
        async with self._lock.held_for(f"{purpose} by {mover.name}"):
            yield

    async def reset_labware_state(self) -> None:
        """Reset this device's labware projection to match the ledger.

        Default: a device holds no labware projection (see class docstring), so
        nothing to reset. Holders with a projection (the LiquidHandler deck)
        override. Satisfies `ILabwareStateHolder`.
        """
        return None

    async def reset_labware_state_everywhere(self) -> None:
        """Reset the projection in every world this device has, not just the
        caller's. Driven by clear-all.

        Default: one projection or none, so this is the same call. A device
        whose projection is per world overrides.
        """
        await self.reset_labware_state()

    def _command_timing(self, command: str) -> CommandTiming | None:
        return collect_command_timings(type(self.driver)).get(command)

    def _engine_time_limit(self, command: str) -> float | None:
        return engine_time_limit(type(self.driver), command)

    def command_max_seconds(self, command: str) -> float | None:
        """The advertised hard upper bound for ``command``, or None if unset.

        The recoverable-timeout dispatcher uses this to bound a device call.
        Read from the driver's ``@command_timing`` metadata.
        """
        timing = self._command_timing(command)
        return timing.max_seconds if timing is not None else None

    def command_typical_seconds(self, command: str) -> float | None:
        """The advertised normal-case duration for ``command``, or None if unset.
        A simulated run paces a step that declares no duration by it."""
        timing = self._command_timing(command)
        return timing.typical_seconds if timing is not None else None

    @property
    def is_initialized(self) -> bool:
        return self.driver.is_initialized

    async def initialize(self) -> None:
        await self.driver.initialize()

    async def connect(self) -> None:
        await self.driver.connect()

    async def disconnect(self) -> None:
        await self.driver.disconnect()

    @property
    def in_use(self) -> bool:
        """True when this device cannot be dispatched against right now.

        Either Orca's device lock is held (an action body is mid driver
        call) OR the hosted gateway has taken external control (an
        operator/AI is troubleshooting). Both states block new
        dispatch; consumers of ``in_use`` (snapshot ``is_busy``,
        ``ResourcePool.available_count``) only need the binary answer
        to "is this device available." Callers that need WHY can read
        ``under_external_control`` directly.

        Pre-S5b ``in_use`` was lock-only. The OR-with-external-control
        makes ``is_busy=False / under_external_control=True`` impossible
        by construction so the snapshot stays internally consistent.
        """
        return self._lock.locked() or self.under_external_control

    @property
    def under_external_control(self) -> bool:
        """True when the device is under external (gateway) control.

        Two things set it and they have different lifetimes. The gateway takes
        it around each operator scope and gives it back when the last open scope
        closes. An operator
        can also HOLD it (``hold_external_control``) across everything they are
        about to do by hand, which is the only way to say "I am driving this,
        keep the workflow off it" for longer than one command.

        The action-dispatch path consults this before acquiring ``self.lock``
        and raises ``DeviceUnderExternalControlError`` while True, so Orca
        workflows back off while a human (or AI) is hands-on with the device.
        The gateway side is the *higher* priority by design -- gateway commands
        never gate on Orca state; Orca always gates on gateway state.
        """
        return self._external_control_scopes > 0 or self._external_control_hold is not None

    @property
    def external_control_hold(self) -> str | None:
        """Why an operator is holding this device, or None if nobody is.

        Empty string when they gave no reason, so "held without a reason" and
        "not held" stay tellable apart.
        """
        return self._external_control_hold

    def take_external_control(self) -> None:
        """Open one operator scope on the device."""
        self._external_control_scopes += 1

    def release_external_control(self) -> None:
        """Close one operator scope. A release with none open does nothing.

        It leaves an operator's standing hold alone: a command sent DURING a
        hold would otherwise hand the device back on its way out.
        """
        self._external_control_scopes = max(0, self._external_control_scopes - 1)

    def hold_external_control(self, reason: str | None = None) -> None:
        """Take this device out of the workflow's reach until it is released."""
        self._external_control_hold = reason or ""

    @property
    def is_offline(self) -> bool:
        """Did not come up at the run's start while another device in its pool did; the pool uses the others."""
        return self._offline

    def mark_offline(self, offline: bool) -> None:
        self._offline = offline

    def release_external_control_hold(self) -> None:
        """Give the device back to the workflow. Idempotent."""
        self._external_control_hold = None

    @property
    def engine_commands(self) -> EngineCommandLog:
        return self._engine_commands

    def use_device_modes(self, modes: IDeviceModes, base_mode: BaseMode = out_of_run) -> None:
        self._run_mode.use_device_modes(modes, base_mode)

    async def setting(self) -> DeviceMode:
        return await self._run_mode.setting()

    def held_labware_id(self) -> str | None:
        """The labware a mover this device owns is holding, such as a deck gripper; None for most devices."""
        return None

    async def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The run mode this device dispatches under given a base mode; see `DeviceRunMode`."""
        return await self._run_mode.mode_under(base)

    async def effective_mode(self) -> WorkflowRunMode:
        """The run mode this device is dispatching under right now; see `DeviceRunMode`."""
        return await self._run_mode.effective_mode()

    @property
    def locations(self) -> List[Location]:
        """Action-reservation candidates: under the flat model, exactly the
        off-graph mutex Location."""
        return self._locations

    def add_location(self, location: Location) -> None:
        if location not in self._locations:
            self._locations.append(location)

    @property
    def site_names(self) -> list[str]:
        return list(self._site_names)

    @property
    def sites(self) -> List[Location]:
        """The device's OWNED labware sites: where its
        plates physically sit, distinct from the reservation mutex."""
        return self._sites

    def add_site(self, location: Location) -> None:
        if location not in self._sites:
            self._sites.append(location)

    @property
    def all_loaded_labware(self) -> List[LabwareInstance]:
        result: List[LabwareInstance] = []
        for loc in self._sites if self._sites else self._locations:
            result.extend(loc.loaded_labware)
        return result

    @property
    def all_loaded_labware_ids(self) -> tuple[str, ...]:
        """What is resident here, for a surface that reports identity.

        A device holds no list of its own any more: this walks its sites, and
        each site answers from the one ledger.
        """
        return tuple(lw.id for lw in self.all_loaded_labware)

    async def _do_prepare_for_place(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("open", self.driver.open)

    async def _do_prepare_for_pick(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("open", self.driver.open)

    async def _do_notify_picked(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("close", self.driver.close)

    async def _do_notify_placed(self, labware: LabwareInstance | None, mover: IPlateMover, target: str | None = None, site: str | None = None) -> None:
        await self.engine_commands.run("close", self.driver.close)

    def __str__(self) -> str:
        return f"Equipment: {self._name}"
    