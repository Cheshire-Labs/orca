from abc import ABC
import logging
from cheshire_drivers.command_timings import collect_command_timings
from cheshire_drivers.interfaces import BaseDriver
from orca.resource_models.resources import IDevice, IModeAware
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.location import Location
from typing import ClassVar, Generic, List, Optional, TypeVar

from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.tracked_lock import TrackedLock
from orca.runtime.run_modes import WorkflowRunMode

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

    def __init__(self,
                 name: str,
                 driver: TDriver,
                 sim_driver: TDriver,
                 sim_override: WorkflowRunMode | None = None,
                 site_names: list[str] | None = None,
                 ) -> None:
        self._name = name
        # Physical labware positions this device owns: one nest by
        # default; multi-position devices (centrifuge buckets) declare theirs.
        self._site_names: list[str] = list(site_names) if site_names else ["slot"]
        # Per-device run-mode override declared at topology construction time.
        # Read by `SystemTopologyRegistry` and surfaced on `TopologyCard.topology_sim_override`
        # so the v3.4 12-row resolver can honor "force this device into sim
        # under an otherwise-LIVE deployment" without bespoke wiring per call
        # site. Threaded onto `SimulationManager` so dispatch routes through
        # the resolver natively.
        self._sim_override = sim_override
        self._sim_manager = SimulationManager(
            driver,
            sim_driver,
            sim_override=sim_override,
        )
        self._is_initialized = False
        self._lock = TrackedLock(f"{name} device lock")
        self._locations: List[Location] = []
        self._sites: List[Location] = []
        # External-control flag: set by a hosted device-integration gateway
        # when an operator or AI sends an ad-hoc troubleshooting command. The
        # action-dispatch path refuses to acquire `self._lock` while this is
        # set, so production workflows can't accidentally race against
        # operator-driven recovery commands. Reason / audit trail lives in
        # operations history + the pause UI; this layer is just the flag.
        self._under_external_control: bool = False
        self._external_control_hold: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def driver(self) -> TDriver:
        return self._sim_manager.driver

    @property
    def live_driver(self) -> TDriver:
        """Live driver, unconditionally - for metadata-only reads.

        See `SimulationManager.live_driver` for the contract: introspection
        routes and topology-card builders read driver-class metadata that
        describes the deployment's actual capability surface; the live
        driver is canonical, the sim driver may declare a different
        interface set. Dispatch callers must keep using `driver`.
        """
        return self._sim_manager.live_driver

    @property
    def lock(self) -> TrackedLock:
        return self._lock

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

    def command_max_seconds(self, command: str) -> float | None:
        """The advertised hard upper bound for ``command``, or None if unset.

        The recoverable-timeout dispatcher uses this to bound a device call.
        Read from the live driver's ``@command_timing`` metadata so the bound
        reflects the deployment's actual hardware, not the sim driver.
        """
        timing = collect_command_timings(type(self.live_driver)).get(command)
        return timing.max_seconds if timing is not None else None

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
        it around each ad-hoc command and gives it straight back. An operator
        can also HOLD it (``hold_external_control``) across everything they are
        about to do by hand, which is the only way to say "I am driving this,
        keep the workflow off it" for longer than one command.

        The action-dispatch path consults this before acquiring ``self.lock``
        and raises ``DeviceUnderExternalControlError`` while True, so Orca
        workflows back off while a human (or AI) is hands-on with the device.
        The gateway side is the *higher* priority by design -- gateway commands
        never gate on Orca state; Orca always gates on gateway state.
        """
        return self._under_external_control or self._external_control_hold is not None

    @property
    def external_control_hold(self) -> str | None:
        """Why an operator is holding this device, or None if nobody is.

        Empty string when they gave no reason, so "held without a reason" and
        "not held" stay tellable apart.
        """
        return self._external_control_hold

    def take_external_control(self) -> None:
        """Mark the device as under external (gateway) control. Idempotent."""
        self._under_external_control = True

    def release_external_control(self) -> None:
        """Clear the per-command external-control flag. Idempotent.

        The gateway calls this when an ad-hoc command finishes. It leaves an
        operator's standing hold alone: a command sent DURING a hold would
        otherwise hand the device back on its way out, which is the opposite of
        what the operator asked for.
        """
        self._under_external_control = False

    def hold_external_control(self, reason: str | None = None) -> None:
        """Take this device out of the workflow's reach until it is released."""
        self._external_control_hold = reason or ""

    def release_external_control_hold(self) -> None:
        """Give the device back to the workflow. Idempotent."""
        self._external_control_hold = None

    @property
    def sim_override(self) -> WorkflowRunMode | None:
        """Per-device run-mode override declared at construction time.

        Surfaced on `TopologyCard.topology_sim_override` via the topology
        registry. The mode resolver consults this between the submit-time
        operator override and the deployment base mode.
        """
        return self._sim_override

    def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The run mode this device dispatches under given a base mode.

        Same answer the driver swap uses; see `SimulationManager`.
        """
        return self._sim_manager.mode_under(base)

    def driver_under(self, base: WorkflowRunMode) -> TDriver:
        """The driver a dispatch under `base` lands on; see `SimulationManager`."""
        return self._sim_manager.driver_under(base)

    @property
    def effective_mode(self) -> WorkflowRunMode:
        """The run mode this device is dispatching under right now.

        Same answer the driver swap uses; see `SimulationManager`.
        """
        return self._sim_manager.effective_mode

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

    async def _do_prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        await self.driver.open()

    async def _do_prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        await self.driver.open()

    async def _do_notify_picked(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        await self.driver.close()

    async def _do_notify_placed(self, labware: LabwareInstance, mover: IPlateMover, target: str | None = None) -> None:
        await self.driver.close()

    def __str__(self) -> str:
        return f"Equipment: {self._name}"
    