from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import typing
from typing import Callable

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.location import Location
from orca.runtime.interfaces import ILabwareStore
from orca.system.thread_manager_interface import IThreadManager
from orca.system.interfaces import IMethodRegistry, IMethodTemplateRegistry, ISystemInfo, IThreadTemplateRegistry, IWorkflowRegistry, IWorkflowTemplateRegistry
from orca.resource_models.labware import LabwareTemplate
from orca.system.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from orca.system.resource_registry import IResourceRegistry
from orca.system.system_map import ILocationRegistry, SystemMap
from orca.system.thread_mutator_interface import IThreadMutator
from orca.system.thread_registry_interface import IThreadRegistry
from orca.system.reservation_manager.errors import IThreadIncidentDeclarer
from orca.state.contents import LabwareContentsLedger
from orca.state.mounted import MountedTipsLedger
from orca.state.ops_history import OpsHistory
from orca.resource_models.tracking_context import TrackingContext
from orca.runtime.run_modes import WorkflowRunMode
from orca.variables.variable_store import VariableService

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


@dataclass(frozen=True)
class DeckReconcileConflict:
    """One labware the ledger and the deck disagree about.

    Nothing here can be resolved automatically, so a human is told and the
    record stays as it is. They resolve it by moving the labware's location
    (it moved) or discharging it (it is gone).
    """
    device_name: str
    labware_id: str
    labware_name: str
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
class LedgerContradiction:
    """An operator command that only makes sense if the record was wrong.

    Not a failure and not a refusal: the command already ran, and the operator
    was the one at the bench. What it leaves behind is a fold nothing can
    repair on its own, because the command says what was true at the positions
    it touched and nothing about the rest of the labware.
    """
    device_name: str
    command: str
    labware_id: str | None
    labware_name: str
    positions: list[str]
    believed: str
    """What the record held instead, in words an operator can act on."""


LedgerContradictionListener = Callable[[LedgerContradiction], None]


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
    def variable_store(self) -> VariableService:
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

    @abstractmethod
    def set_executing_workflow_factory_refs(self, labware_store: ILabwareStore) -> None:
        """Inject the runtime's labware_store into the executing-workflow
        factory so reuse-bind threads can dual-register fresh labware."""
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
    async def ensure_runtime_initialized(
        self, workflow: WorkflowTemplate | None = None,
    ) -> None:
        """Lazy first-thread-touch device init.

        See `System.ensure_runtime_initialized` for the full contract.
        Execution entrypoints invoke this after seeding
        `current_run_mode`; the first call under each run mode
        configures LiquidHandler decks and initializes fresh non-sim
        device worlds, later calls under that mode no-op.
        """
        raise NotImplementedError

    @abstractmethod
    def forget_bringup(self, device_name: str) -> None:
        """Drop the record that this device's worlds were brought up.

        Called when the device bridge holding it rebuilt its drivers, so the
        next bring-up walk covers it again. See `System.forget_bringup`.
        """
        raise NotImplementedError

    @abstractmethod
    async def reconcile_lh_deck_occupancy(self, device_location: Location) -> None:
        """Rebuild a liquid handler's whole driver deck projection from the ledger.

        No-op for non-LiquidHandler / unconfigured device locations. For a
        driver world whose occupancy is unknown (freshly configured, a rebuilt
        session, a boot): it clears the deck and re-places every occupant. A
        change that concerns one labware belongs on
        `project_labware_on_lh_decks`. See `System.reconcile_lh_deck_occupancy`.
        """
        raise NotImplementedError

    @abstractmethod
    async def project_labware_on_lh_decks(
        self, labware: LabwareInstance, *locations: Location,
    ) -> None:
        """Make each liquid-handler deck these locations touch agree about ONE labware.

        Drives every single-labware push: an operator placement or relocation, a
        thread arriving on a deck site, a discharge, and the on-deck re-seed
        behind an operator state edit (set-volume, set-tip-state). Whatever else
        stands on those decks is left alone. No-op for non-LiquidHandler /
        unconfigured device locations. See `System.project_labware_on_lh_decks`.
        """
        raise NotImplementedError

    @abstractmethod
    async def retract_labware_from_lh_decks(self, labware: LabwareInstance) -> None:
        """Take ONE labware off every liquid-handler deck, in every laid-out world.

        A discharge means the labware is on no deck at all, so this asks every
        handler rather than trusting the engine's record of where it was.
        Best-effort per handler: a handler that cannot answer is logged, never
        raised, so a recovery verb still completes. See
        `System.retract_labware_from_lh_decks`.
        """
        raise NotImplementedError

    @abstractmethod
    async def retract_labware_from_other_lh_worlds(
        self, labware: LabwareInstance,
    ) -> None:
        """Take ONE labware off every liquid-handler deck world but the caller's.

        What a move needs: the caller projects the new position into its own
        world, and the others only have to stop holding the old one. See
        `System.retract_labware_from_other_lh_worlds`.
        """
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
    def notify_ledger_contradiction(
        self, contradiction: "LedgerContradiction",
    ) -> None:
        """Tell every listener that an operator command disagreed with the fold.

        Public for the same reason the deck one is: the disagreement is found
        while following an operator's own command, which is not the engine's
        path, and the surface that reports it is not this module's business.
        """
        raise NotImplementedError

    @abstractmethod
    def add_ledger_contradiction_listener(
        self, listener: LedgerContradictionListener,
    ) -> None:
        """Observe operator commands that disagree with the fold."""
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