from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import typing
from collections.abc import Sequence
from typing import Callable, Mapping

from orca.gateway.device_fault import DeviceFault

from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_catalog_protocol import ILabwareCatalog
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.command_effects import SystemCommandPipeline
from orca.resource_models.location import Location
from orca.resource_models.labware_service_interface import ILabwareService
from orca.system.device_worlds import DeviceWorlds
from orca.system.errors import DeviceInitializationError
from orca.workflow_models.labware_threads.thread_manager_interface import IThreadManager
from orca.system.interfaces import IMethodTemplateRegistry, ISystemInfo, IThreadTemplateRegistry, IWorkflowTemplateRegistry
from orca.workflow_models.workflows.registry_interfaces import IMethodRegistry, IWorkflowRegistry
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from orca.resource_models.resource_registry import IResourceRegistry
from orca.system.system_map import ILocationRegistry, SystemMap
from orca.system.thread_mutator_interface import IThreadMutator
from orca.system.reservation_manager.thread_registry_interface import IThreadRegistry
from orca.system.reservation_manager.errors import IThreadIncidentDeclarer
from orca.workflow_models.thread_status_recorder import IThreadStatusRecorder
from orca.state.contents import LabwareContentsLedger
from orca.state.mounted import MountedTipsLedger
from orca.state.ops_history import OpsHistory
from orca.resource_models.tracking_context import TrackingContext
from orca.modes.run_modes import WorkflowRunMode
from orca.variables.variable_store import IVariableService

from orca.workflow_models.labware_threads.executing_labware_thread import IExecutingThreadRegistry
from orca.workflow_models.workflows.executing_workflow import IExecutingWorkflowRegistry
from orca.workflow_models.workflow_templates import WorkflowTemplate

from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.workflow_registry import IExecutingMethodRegistry


class DeckConflictReason(Enum):
    """Why the ledger and a liquid handler's deck cannot both be right.

    The ledger stays authoritative in every one of these; the operator decides
    which side to correct. Nothing here self-heals.
    """

    SITE_NOT_IN_LAYOUT = "site_not_in_layout"
    """The ledger places it at a site the active deck layout no longer provides."""

    HELD_BY_GRIPPER = "held_by_gripper"
    """The ledger has it in the jaws, which is at no deck site to project to."""

    DRIVER_SITE_DIFFERS = "driver_site_differs"
    """Both place it, at different sites. Something moved it outside orca."""

    MISSING_FROM_DRIVER = "missing_from_driver"
    """The ledger places it on this deck and the driver has it nowhere."""

    UNKNOWN_TO_LEDGER = "unknown_to_ledger"
    """The driver has labware on a deck site that the ledger does not know."""

    INTERRUPTED_MOVE = "interrupted_move"
    """A gripper move raised partway; the driver still names the site it began at."""

    LEDGER_TARGET_OCCUPIED = "ledger_target_occupied"
    """Labware arrived where the ledger already has different labware."""

    CONTENTS_DIFFER = "contents_differ"
    """Both agree where it is and disagree about what is in it. The driver holds
    what orca last projected onto it, so this usually means the driver's session
    was rebuilt underneath us -- but the ledger cannot see a hand either, so
    neither side settles it alone."""


NOTHING_ON_RECORD_WAS_MOVED = (
    "Nothing on record was being moved, so what the gripper holds is unknown. "
    "Look at the gripper and take out anything it holds, then settle the deck with reconcile-deck."
)


@dataclass(frozen=True)
class DeckReconcileConflict:
    """One thing the ledger and the deck disagree about.

    Nothing here can be resolved automatically, so a human is told and the
    record stays as it is. They resolve it by moving the labware's location
    (it moved) or discharging it (it is gone).
    """
    device_name: str
    labware_id: str | None
    labware_name: str | None
    """Both None for an interrupted gripper move that named no labware: nothing on
    record was being moved, and what the gripper holds is unknown."""
    position_id: str
    reason: DeckConflictReason
    driver_site: str | None = None
    """Where the DRIVER has it, in the form its own commands take. None when the
    driver has it nowhere, or when the driver was never asked."""
    blocking_labware_name: str | None = None
    """The labware already recorded at ``position_id``, when an existing record
    is what the two sides disagree over. None for every other reason."""
    detail: str | None = None
    """What the two sides each say, in words, when the reason needs it."""


DeckReconcileConflictListener = Callable[[DeckReconcileConflict], None]


@dataclass(frozen=True)
class DeckComparison:
    """What a liquid handler's driver says its deck holds, against the ledger.

    ``driver_deck_empty`` separates the two ways they disagree. A driver whose
    session was rebuilt reports NOTHING, which is not a disagreement about where
    anything is: it is a deck waiting to be re-declared, and the reconcile does
    that. Anything else is a genuine conflict a human has to settle.
    """
    device_name: str
    driver_deck_empty: bool
    disagreements: tuple[DeckReconcileConflict, ...]
    interrupted_move_labware: str | None = None
    record_deck_empty: bool = False
    """The record has no labware on this deck or in its gripper."""

    @property
    def agrees(self) -> bool:
        """An empty driver deck agrees only with a record that has nothing there either."""
        if self.driver_deck_empty and not self.record_deck_empty:
            return False
        return not self.disagreements


class ISystem(ISystemInfo,
              IResourceRegistry,
              ILabwareRegistry,
              ILabwareTemplateRegistry,
              IWorkflowTemplateRegistry,
              IMethodTemplateRegistry,
              IThreadTemplateRegistry,
              ILocationRegistry,
              IMethodRegistry,
              IThreadRegistry,
              IExecutingThreadRegistry,
              IWorkflowRegistry,
              IExecutingWorkflowRegistry,
              IThreadManager,
              IExecutingMethodRegistry,
              IThreadMutator,
              ABC):

    @property
    @abstractmethod
    def system_map(self) -> SystemMap:
        raise NotImplementedError

    @property
    @abstractmethod
    def variable_store(self) -> IVariableService:
        raise NotImplementedError

    @property
    @abstractmethod
    def labware_catalog(self) -> ILabwareCatalog:
        raise NotImplementedError

    @abstractmethod
    def resolve_journey_location(self, name: str) -> Location:
        """The location a thread's start or end name means; a device name maps to its single site."""
        raise NotImplementedError

    @property
    @abstractmethod
    def tracking_context(self) -> TrackingContext:
        raise NotImplementedError

    @property
    @abstractmethod
    def ops_history(self) -> OpsHistory:
        raise NotImplementedError

    @property
    @abstractmethod
    def labware_contents(self) -> LabwareContentsLedger:
        """The one place that answers what a labware holds."""
        raise NotImplementedError

    @abstractmethod
    def handlers_with_a_head(self) -> list[str]:
        """Every liquid handler, with a modelled deck or not: each carries a head the record keeps."""
        raise NotImplementedError

    @property
    @abstractmethod
    def mounted_tips(self) -> MountedTipsLedger:
        """What each head is carrying, folded from the record."""
        raise NotImplementedError

    @property
    @abstractmethod
    def labware_location_service(self) -> ILabwareLocationService:
        """The live labware location tracker.

        Exposed so facades (LabwareFacade) can wrap it without reaching into
        internal builder-constructed state.
        """
        raise NotImplementedError

    @abstractmethod
    def labware_at_device(self, device_name: str) -> list[LabwareInstance]:
        """The labware standing at a device and its sites: what a command that names none of its own is about."""
        raise NotImplementedError

    @abstractmethod
    async def note_no_answer(
        self, device_name: str, execution_id: str | None, named: Sequence[str], command: str,
    ) -> None:
        """What a command did at this device is unknown: the labware it names, and the head for a tip command, are unobserved until stated.

        A command that names no labware (`named` is empty) is about everything standing at the device.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def command_pipeline(self) -> SystemCommandPipeline:
        """The one pipeline every device command in this system goes through."""
        raise NotImplementedError

    @property
    @abstractmethod
    def labware_placer(self) -> LabwarePlacer:
        """The placement chokepoint: records a plate at a location across every
        position holder (slot, bridge loaded-list, ledger, LH deck, transporter
        graph). Exposed so facades / engine paths place through one writer."""
        raise NotImplementedError

    @abstractmethod
    async def ensure_adhoc_labware_template(
        self, labware_type: str,
    ) -> LabwareTemplate:
        """The template for a catalog labware type nothing declared, minting it
        on first use.

        The door for labware a deployment package never anticipated: everything
        keyed off a labware needs a template, so an operator naming a catalog
        type gets one derived from the catalog row rather than a refusal.
        See ``orca.resource_models.adhoc_labware``.
        """
        raise NotImplementedError

    @abstractmethod
    async def create_and_register_thread_instance(
        self,
        template: "ThreadTemplate",
        shared_method: "ExecutingMethod | None" = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> "LabwareThreadInstance":
        raise NotImplementedError

    @property
    @abstractmethod
    def labware_service(self) -> ILabwareService:
        """The labware record in the database. The placer saves every position to it, and reuse-bind threads
        register fresh labware in it."""
        raise NotImplementedError

    @abstractmethod
    def set_thread_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Inject the runtime's deadlock-declarer back-reference into the
        executing-thread factory so the typed UnresolvableDeadlockError
        catch in each thread can record IncidentCategory.UNRESOLVABLE_DEADLOCK
        and fan out execution-wide pause. Called by SystemRuntime.__init__
        post-construction (S3 R1.5)."""
        raise NotImplementedError

    @abstractmethod
    def set_thread_status_recorder(self, recorder: IThreadStatusRecorder) -> None:
        """Give every thread minted after this the runtime's run-status recorder."""
        raise NotImplementedError

    @abstractmethod
    async def bring_up_for_a_run(self, unreachable: Mapping[str, str]) -> list[DeviceInitializationError]:
        """Bring up every device and every transporter for a fresh run; DevicesNotReadyError refuses it.

        `unreachable` names devices whose orca-client is not connected. Returns the failures tolerated in pools.
        """

    @abstractmethod
    def read_faults_from(self, fault_of: Callable[[str], DeviceFault | None]) -> None:
        """Where a bring-up reads a device's standing fault. A faulted device is not brought up, so its fault stands."""

    @abstractmethod
    async def ensure_runtime_initialized(self) -> None:
        """Lazy first-thread-touch device init.

        See `System.ensure_runtime_initialized` for the full contract.
        Execution entrypoints invoke this after seeding
        `current_run_mode`; the first call under each run mode
        configures LiquidHandler decks and initializes fresh non-sim
        device worlds, later calls under that mode no-op.
        """
        raise NotImplementedError

    @abstractmethod
    def refuse_a_join_unless_its_devices_are_reachable(self, unreachable: Mapping[str, str]) -> None:
        """Refuse a submission joining a running run when a device the run uses has lost its orca-client.

        Brings nothing up: the run's start did that, and its threads may be using the devices now.
        """
        raise NotImplementedError

    @abstractmethod
    def device_in_use_by(self, device_name: str) -> str | None:
        """Who holds the device right now, in operator words, or None when it is free."""
        raise NotImplementedError

    @abstractmethod
    async def bring_up_device(self, device_name: str) -> None:
        """Bring one device up in the world it now resolves to, as a run's start does: lay out its deck, then initialize it."""
        raise NotImplementedError

    @abstractmethod
    def forget_bringup(self, device_name: str) -> None:
        """Drop the record that this device's worlds were brought up.

        Called when the device bridge holding it rebuilt its drivers, so the
        next bring-up walk covers it again. See `System.forget_bringup`.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def device_worlds(self) -> DeviceWorlds:
        raise NotImplementedError

    @abstractmethod
    async def lh_deck_projection(
        self, device_location: Location,
    ) -> tuple[DeckLayoutConfig, list[DeckResourceConfig]] | None:
        """A liquid handler's deck layout and where the ledger places each labware on it, without what each holds; None when it has no layout."""
        raise NotImplementedError

    @abstractmethod
    async def compare_lh_deck_occupancy(
        self, device_location: Location,
    ) -> "DeckComparison | None":
        """Ask a liquid handler's driver what its deck holds, against the ledger.

        Reads only; the ledger stays authoritative whatever comes back. None
        for a location that is not a configured liquid handler.
        """
        raise NotImplementedError

    @abstractmethod
    def notify_deck_reconcile_conflict(
        self, conflict: "DeckReconcileConflict",
    ) -> None:
        """Tell every listener about one labware the ledger and a deck disagree
        about. Public because the comparison is driven from the device facade,
        which is where the operator's own compare / reconcile verbs live."""
        raise NotImplementedError

    @abstractmethod
    def add_deck_reconcile_conflict_listener(
        self, listener: DeckReconcileConflictListener,
    ) -> None:
        """Observe labware the ledger and the deck disagree about. Fired once
        per labware per pass, whatever the reason."""
        raise NotImplementedError