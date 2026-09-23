import asyncio
import logging
from types import MappingProxyType
from typing import Awaitable, Callable, List, Sequence
from orca.devices.devices import DeckLabwareIdentityError, LiquidHandler
from orca.resource_models.devices import Device
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.resources import IInitializable, IModeAware, IResource

from orca.resource_models.adhoc_labware import (
    adhoc_template_name,
    build_adhoc_template,
)
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.state.contents import LabwareContentsLedger
from orca.resource_models.labware_directory import reset_labware_directory
from orca.state.current import reset_placement_ledger
from orca.state.mounted import MountedTipsLedger
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig
from cheshire_drivers.liquid_handler_models import (
    DeckResourceState,
    GetDeckStateRequest,
    InterruptedMove,
    ReconcileDeckOccupancyRequest,
)
from orca.runtime.interfaces import ILabwareStore
from orca.system.deck_sites import enumerate_deck_sites
from orca.system.reservation_manager.errors import IThreadIncidentDeclarer
from orca.runtime.labware_catalog_protocol import ILabwareCatalog
from orca.runtime.labware_group import LabwareGroup
from orca.runtime.run_modes import WorkflowRunMode, current_run_mode
from orca.runtime.submission import ResolvedAcquisition
from orca.runtime.submission_modes import BatchMode
from orca.events.execution_context import WorkflowExecutionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.resource_models.transporter import Transporter
from orca.resource_models.transporter_base import TransporterBase
from orca.system.system_info import SystemInfo
from orca.system.errors import DeviceInitializationError
from orca.system.system_interface import (
    DeckComparison,
    DeckConflictReason,
    DeckReconcileConflict,
    DeckReconcileConflictListener,
    LedgerContradiction,
    LedgerContradictionListener,
    ISystem,
)
from orca.system.interfaces import IMethodRegistry
from orca.system.interfaces import IWorkflowRegistry
from orca.system.resource_registry import IResourceRegistry, IResourceRegistryObserver
from orca.system.system_map import SystemMap
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread, IExecutingThreadRegistry
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow, IExecutingWorkflowRegistry
from orca.workflow_models.workflows.workflow_factories import ThreadFactory
from orca.system.thread_registry_interface import IThreadRegistry
from orca.state.ops_history import OpsHistory
from orca.resource_models.tracking_context import TrackingContext
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate
from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.workflows.workflow_registry import IExecutingMethodRegistry
from orca.system.mutation.coordinator import MutationCoordinator
from orca.variables.deployment_profile import DeploymentProfile
from orca.variables.errors import OptionValue
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.resource_models.labware_placement import LabwarePlacer
from orca.variables.variable_store import VariableService, VariableStore
from orca.workflow_models.mutation_position import InsertPosition

orca_logger = logging.getLogger("orca")


class System(ISystem):
    def __init__(self,
                 info: SystemInfo,
                 system_map: SystemMap,
                 resource_registry: IResourceRegistry,
                 template_registry: TemplateRegistry,
                 labware_registry: LabwareRegistry,
                 thread_registry: IThreadRegistry,
                 executing_method_registry: IExecutingMethodRegistry,
                 executing_thread_registry: IExecutingThreadRegistry,
                 thread_factory: ThreadFactory,
                 thread_manager: IThreadManager,
                 method_registry: IMethodRegistry,
                 workflow_registry: IWorkflowRegistry,
                 executing_workflow_registry: IExecutingWorkflowRegistry,
                 tracking_context: TrackingContext,
                 ops_history: OpsHistory,
                 labware_location_service: ILabwareLocationService,
                 labware_catalog: ILabwareCatalog,
                 variable_store: VariableService | None = None) -> None:
        self._info = info
        self._resources = resource_registry
        # A build owns its world. Without this a rebuilt runtime inherits the
        # dead build's occupancy and refuses the first plate onto a pad,
        # naming a labware that no longer exists.
        reset_placement_ledger()
        reset_labware_directory()
        self._system_map = system_map
        self._templates = template_registry
        self._labwares = labware_registry
        self._method_registry = method_registry
        self._workflow_registry = workflow_registry
        self._thread_manager = thread_manager
        self._thread_registry = thread_registry
        self._thread_factory = thread_factory
        self._executing_method_registry = executing_method_registry
        self._executing_thread_registry = executing_thread_registry
        self._executing_workflow_registry = executing_workflow_registry
        self._tracking_context = tracking_context
        self._ops_history = ops_history
        self._labware_contents = LabwareContentsLedger(ops_history)
        self._mounted_tips = MountedTipsLedger(ops_history)
        self._labware_location_service = labware_location_service
        self._labware_placer = LabwarePlacer(
            labware_location_service, self.project_labware_on_lh_decks,
        )
        # The thread factory needs the same one. Pushing it from the caller
        # left every entry point but SystemRuntime with threads that had
        # nowhere to record a move.
        executing_thread_registry.set_labware_placer(self._labware_placer)
        self._labware_catalog = labware_catalog
        self._variable_store: VariableService = variable_store or VariableService(VariableStore())
        self._mutation_coordinator = MutationCoordinator(
            method_registry=method_registry,
            executing_method_registry=executing_method_registry,
            thread_template_registry=template_registry,
            thread_manager=thread_manager,
        )
        # Lazy first-thread-touch device init: see `ensure_runtime_initialized`.
        # Keyed by (mode, scope): a workflow declaring only the liquid handler
        # must not mark the arm's bring-up done for the next one.
        self._lazy_init_done: set[
            tuple[WorkflowRunMode, frozenset[str] | None]
        ] = set()
        self._worlds_pending_reconcile: set[tuple[str, WorkflowRunMode]] = set()
        self._deck_reconcile_conflict_listeners: list[DeckReconcileConflictListener] = []
        self._ledger_contradiction_listeners: list[LedgerContradictionListener] = []
        self._initialized_worlds: set[tuple[str, WorkflowRunMode]] = set()
        self._lazy_init_lock = asyncio.Lock()

    @property
    def id(self) -> str:
        return self._info.id

        
    @property
    def name(self) -> str:
        return self._info.name

    @property
    def version(self) -> str:
        return self._info.version

    @property
    def description(self) -> str:
        return self._info.description
    
    @property
    def system_map(self) -> SystemMap:
        return self._system_map

    @property
    def tracking_context(self) -> TrackingContext:
        return self._tracking_context

    @property
    def ops_history(self) -> OpsHistory:
        return self._ops_history

    @property
    def mounted_tips(self) -> MountedTipsLedger:
        """What each head is carrying, folded from the record."""
        return self._mounted_tips

    @property
    def labware_contents(self) -> LabwareContentsLedger:
        """The one place that answers what a labware holds."""
        return self._labware_contents

    @property
    def labware_location_service(self) -> ILabwareLocationService:
        return self._labware_location_service

    @property
    def labware_placer(self) -> LabwarePlacer:
        """The single chokepoint for recording a plate at a location.

        Every placement path (operator place, thread-start-on-deck, reuse-bind,
        boot-rehydrate) routes through it."""
        return self._labware_placer

    @property
    def labware_catalog(self) -> ILabwareCatalog:
        """The runtime labware catalog used to materialize labware instances.

        Workflows registered after the System is constructed (the dynamic
        ``add_workflow_template`` path used by a hosted deployment's submission service)
        consult this property to bind the catalog onto each labware
        template they introduce, mirroring what ``SdkToSystemBuilder``
        does for templates supplied at build time.
        """
        return self._labware_catalog

    @property
    def locations(self) -> List[Location]:
        return self._system_map.locations

    @property
    def labwares(self) -> List[LabwareInstance]:
        return self._labwares.labwares
    
    @property
    def resources(self) -> List[IResource]:
        return self._resources.resources

    @property
    def devices(self) -> List[Device]:
        return self._resources.devices
    
    @property
    def transporters(self) -> List[Transporter]:
        return self._resources.transporters

    @property
    def movers(self) -> List[TransporterBase]:
        return self._resources.movers
    
    @property
    def resource_pools(self) -> List[ResourcePool]:
        return self._resources.resource_pools
    
    @property
    def threads(self) -> List[LabwareThreadInstance]:
        return self._thread_registry.threads

    def get_resource(self, name: str) -> IResource:
        return self._resources.get_resource(name)

    def get_device(self, name: str) -> Device:
        return self._resources.get_device(name)

    def get_transporter(self, name: str) -> Transporter:
        return self._resources.get_transporter(name)

    def get_resource_pool(self, name: str) -> ResourcePool:
        return self._resources.get_resource_pool(name)

    def get_location(self, name: str) -> Location:
        return self._system_map.get_location(name)

    def resolve_journey_location(self, name: str) -> Location:
        """Thread start=/end= resolution: a device name maps to its single
        site, never the reservation mutex."""
        return self._system_map.resolve_journey_location(name)

    def get_labware(self, name: str) -> LabwareInstance:
        return self._labwares.get_labware(name)

    def add_resource(self, resource: IResource) -> None:
        self._resources.add_resource(resource)

    async def add_location(self, location: Location) -> None:
        await self._system_map.add_location(location)

    def add_resource_pool(self, resource_pool: ResourcePool) -> None:
        self._resources.add_resource_pool(resource_pool)

    def add_labware(self, labware: LabwareInstance) -> None:
        # Registration only. `seed_at_birth` is the single binder, so a labware
        # that skipped it stays mute and raises on the first read instead of
        # answering from the driver's own defaults.
        self._labwares.add_labware(labware)

    def remove_labware(self, labware_id: str) -> LabwareInstance | None:
        """Remove a labware instance from the system registry by id.

        Used by the operator clear surfaces. Returns the removed
        instance if found, ``None`` if no labware with that id exists.
        """
        return self._labwares.remove_labware(labware_id)

    def get_labware_template(self, name: str) -> LabwareTemplate:
        return self._labwares.get_labware_template(name)

    def add_labware_template(self, labware: LabwareTemplate) -> None:
        self._labwares.add_labware_template(labware)

    async def ensure_adhoc_labware_template(
        self, labware_type: str,
    ) -> LabwareTemplate:
        """The template for a catalog labware type nothing declared, minting it
        on first use.

        Idempotent by name, so the second operator to place a trough of the same
        type gets the template the first one caused, and a rebuild that re-derives
        one from a persisted instance lands on the same object every other layer
        already looks up.
        """
        name = adhoc_template_name(labware_type)
        existing = self._derived_template_named(name, labware_type)
        if existing is not None:
            return existing
        template = await build_adhoc_template(self._labware_catalog, labware_type)
        # Re-check: the build awaits the catalog, so a concurrent register of
        # the same type can have landed one meanwhile. Keeping the first keeps
        # every instance of that type pointing at one template object.
        settled = self._derived_template_named(name, labware_type)
        if settled is not None:
            return settled
        self.add_labware_template(template)
        return template

    def _derived_template_named(
        self, name: str, labware_type: str,
    ) -> LabwareTemplate | None:
        """The registered template for a derived name, if it really is that one.

        A declared template is free to take the name a derivation would produce.
        Returning it would mint the wrong labware under the right name and send
        the wrong geometry to the deck, so a mismatch is refused rather than
        used.
        """
        try:
            existing = self.get_labware_template(name)
        except KeyError:
            return None
        if existing.labware_type == labware_type:
            return existing
        raise ValueError(
            f"template {name!r} is already declared with labware type "
            f"{existing.labware_type!r}, so it cannot stand for labware type "
            f"{labware_type!r}. Register it by template_name, or rename the "
            f"declared template."
        )

    @property
    def labware_templates(self) -> List[LabwareTemplate]:
        return self._labwares.labware_templates
    
    def get_labware_thread_template(self, workflow_name: str, name: str) -> ThreadTemplate:
        return self._templates.get_labware_thread_template(workflow_name, name)

    def get_labware_thread_templates(self) -> MappingProxyType[tuple[str, str], ThreadTemplate]:
        return self._templates.get_labware_thread_templates()

    def add_labware_thread_template(self, workflow_name: str, labware_thread: ThreadTemplate) -> None:
        self._templates.add_labware_thread_template(workflow_name, labware_thread)

    def get_method_templates(self) -> MappingProxyType[tuple[str, str], MethodTemplate]:
        return self._templates.get_method_templates()

    def get_method_template(self, workflow_name: str, name: str) -> MethodTemplate:
        return self._templates.get_method_template(workflow_name, name)

    def add_method_template(self, workflow_name: str, method: MethodTemplate) -> None:
        self._templates.add_method_template(workflow_name, method)

    def get_workflow_templates(self) -> MappingProxyType[str, WorkflowTemplate]:
        return self._templates.get_workflow_templates()

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._templates.get_workflow_template(name)
    
    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        self._templates.add_workflow_template(workflow)

    def set_executing_workflow_factory_refs(
        self, labware_store: ILabwareStore,
    ) -> None:
        """Inject the runtime's labware_store into the executing-workflow
        factory so reuse-bind threads can register fresh labware into both
        stores. Called by SystemRuntime.__init__ post-construction; the
        factory's `system` ref is filled with `self`.
        """
        if hasattr(self._executing_workflow_registry, "set_runtime_refs"):
            self._executing_workflow_registry.set_runtime_refs(
                labware_store, self,
            )

    def set_thread_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Inject the runtime's thread-incident-declarer back-reference into
        the executing-thread factory.

        Called by SystemRuntime.__init__ once it has constructed itself.
        Threads spawned after this point receive the back-ref so they can
        record UNRESOLVABLE_DEADLOCK (typed catch) and ACTION_FAILED
        (default-PAUSE action error) incidents that surface on every
        operator surface.

        Calls unconditionally -- the method is part of the
        ``IExecutingThreadRegistry`` contract, so a registry that cannot
        satisfy it is a mis-wiring that should fail loudly here rather
        than silently disabling incident recording.
        """
        self._executing_thread_registry.set_thread_incident_declarer(declarer)

    def set_labware_placer(self, placer: LabwarePlacer) -> None:
        """Forward to the executing-thread factory. Part of
        IExecutingThreadRegistry; the constructor already pushes the system's
        own placer, so this is for a caller substituting a different one."""
        self._executing_thread_registry.set_labware_placer(placer)

    def remove_workflow_template(self, name: str) -> WorkflowTemplate | None:
        return self._templates.remove_workflow_template(name)

    def remove_method_template(self, workflow_name: str, name: str) -> MethodTemplate | None:
        return self._templates.remove_method_template(workflow_name, name)

    def remove_labware_thread_template(self, workflow_name: str, name: str) -> ThreadTemplate | None:
        return self._templates.remove_labware_thread_template(workflow_name, name)

    def get_workflow(self, id: str) -> WorkflowInstance:
        return self._workflow_registry.get_workflow(id)
    
    def add_workflow(self, workflow: WorkflowInstance) -> None:
        self._workflow_registry.add_workflow(workflow)

    def get_executing_workflow(self, execution_id: str) -> ExecutingWorkflow:
        return self._executing_workflow_registry.get_executing_workflow(execution_id)

    def get_thread(self, id: str) -> LabwareThreadInstance:
        return self._thread_registry.get_thread(id)
    
    def get_thread_by_labware(self, labware_id: str) -> LabwareThreadInstance:
        return self._thread_registry.get_thread_by_labware(labware_id)

    def add_thread(self, labware_thread: LabwareThreadInstance) -> None:
        self._thread_registry.add_thread(labware_thread)

    async def create_and_register_thread_instance(
        self,
        template: ThreadTemplate,
        shared_method: ExecutingMethod | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> LabwareThreadInstance:
        thread = await self._thread_factory.create_instance(
            template, run_mode=run_mode, shared_method=shared_method,
        )
        self._thread_registry.add_thread(thread)
        return thread
    
    def get_executing_method(self, id: str) -> ExecutingMethod:
        return self._executing_method_registry.get_executing_method(id)
    
    def create_executing_method(self, method_id: str, context: WorkflowExecutionContext) -> ExecutingMethod:
        return self._executing_method_registry.create_executing_method(method_id, context)

    def create_executing_thread(self, thread_id: str, context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        return self._executing_thread_registry.create_executing_thread(thread_id, context)
    
    def get_executing_thread(self, thread_id: str) -> ExecutingLabwareThread:
        return self._executing_thread_registry.get_executing_thread(thread_id)

    def get_method(self, id: str) -> IMethod:
        return self._method_registry.get_method(id)
    
    def add_method(self, method: IMethod) -> None:
        self._method_registry.add_method(method)

    def has_resource(self, name: str) -> bool:
        return self._resources.has_resource(name)

    def add_observer(self, observer: IResourceRegistryObserver) -> None:
        return self._resources.add_observer(observer)
    
    def create_and_register_method_instance(self, template: MethodTemplate) -> IMethod:
        return self._method_registry.create_and_register_method_instance(template)
        
    async def create_and_register_workflow_instance(
        self,
        template: WorkflowTemplate,
        submission_id: str | None = None,
        groups: Sequence[LabwareGroup] | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] | None = None,
        id: str | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> WorkflowInstance:
        return await self._workflow_registry.create_and_register_workflow_instance(
            template, submission_id=submission_id, groups=groups,
            batch_mode=batch_mode, resolved_acquisitions=resolved_acquisitions,
            id=id,
            run_mode=run_mode,
        )

    async def build_entry_threads_for(
        self,
        template: WorkflowTemplate,
        submission_id: str,
        groups: Sequence[LabwareGroup],
        batch_mode: BatchMode = BatchMode.STANDALONE,
        resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> List[LabwareThreadInstance]:
        """Build new entry threads for mid-run submission injection.

        Threads are not registered yet; caller must call add_thread() for
        each before wrapping them via an ExecutingWorkflow.
        """
        return await self._workflow_registry.build_entry_threads_for(
            template, submission_id, groups, batch_mode=batch_mode,
            resolved_acquisitions=resolved_acquisitions,
            run_mode=run_mode,
        )
    
    def has_completed(self) -> bool:
        return self._thread_manager.has_completed()

    @property
    def executing_threads(self) -> List[ExecutingLabwareThread]:
        return self._thread_manager.executing_threads

    @property
    def active_threads(self) -> List[ExecutingLabwareThread]:
        return self._thread_manager.active_threads

    async def start_all_threads(self) -> None:
        return await self._thread_manager.start_all_threads()

    def stop_all_threads(self) -> None:
        return self._thread_manager.stop_all_threads()
    
    async def initialize_all(self) -> None:
        """ Initializes all resources in the system. This is typically called at the start of a workflow or method execution."""
        return await self._resources.initialize_all()

    async def ensure_runtime_initialized(
        self, workflow: WorkflowTemplate | None = None,
    ) -> None:
        """Lazy first-execution-entry device init, once per run mode and scope.

        Called from each execution entrypoint (`SystemRuntime._run_workflow`,
        `WorkflowExecutor.start`, `StandaloneMethodExecutor.start`) after
        `current_run_mode` is seeded; the first call under a given mode walks
        every LiquidHandler to push its deck config and reconcile occupancy,
        then initializes the fresh non-sim device worlds in play. Later
        calls under the same mode and scope observe `_lazy_init_done` and
        return.

        `workflow` narrows the device init to what that workflow declares
        (see `_resources_in_play`); the deck walk is not narrowed, because
        laying a deck out moves nothing. Bringing an arm up takes its session
        and resets
        what it was tracking, and a workflow that never moves labware between
        devices has no business doing that to one: on the bench an unscoped
        walk drove a transporter into a neighbouring instrument during a run
        that only used the liquid handler, back when bring-up homed. Passing
        None keeps the old whole-topology walk, which is what a caller holding
        no workflow has to do.

        Done-ness is per WORLD, not per process: the mode picks which world
        a dispatch reaches (PURE_SIM the local sim driver; DEVICE_SIM and
        LIVE the wire, whose backend the on-prem device bridge swaps per
        stamped effective mode). A PURE_SIM dry run therefore must not satisfy a
        later LIVE run -- the live instrument would start with no deck and
        no device init. The per-device gates key on the resolved per-device
        mode (see `deck_world_layout` and `_initialize_fresh_worlds`),
        so a `sim_override`-pinned device is neither reconfigured, nor
        re-reconciled, nor re-initialized by a second submission mode that
        resolves it to the same world.

        The walk also survives partial failure: the (mode, scope) is marked
        done only when the whole walk succeeds, a freshly configured world keeps
        a pending-reconcile obligation until a reconcile completes (a
        failed reconcile is retried on the next entry, even though the
        configure itself is not repeated), and each device world is marked
        initialized individually so a retry only re-initializes the
        devices that failed.

        Lock-guarded because concurrent task entry can interleave between
        the flag check and the configure await: under asyncio one coroutine
        yields during the `configure_deck` / initialize await, so a
        sibling task could re-enter before the mode is recorded. The lock
        serializes the check + walk so exactly one caller does the work.

        Caller must seed `current_run_mode` before invoking; reads outside
        a seeded context raise LookupError.
        """
        mode = current_run_mode.get()
        in_play = self._resources_in_play(workflow)
        key = (mode, in_play)
        if key in self._lazy_init_done:
            return
        async with self._lazy_init_lock:
            if key in self._lazy_init_done:
                return
            for resource in self._resources.resources:
                if isinstance(resource, LiquidHandler):
                    if await self._deck_world_layout(resource) is None:
                        continue
                    world = (resource.name, resource.effective_mode)
                    if self._deck_world_owes_a_reconcile(resource):
                        await self.reconcile_lh_deck_occupancy(
                            self._system_map.get_resource_location(resource.name)
                        )
                        self._worlds_pending_reconcile.discard(world)
            await self._initialize_fresh_worlds(in_play)
            self._lazy_init_done.add(key)

    def _resources_in_play(
        self, workflow: WorkflowTemplate | None,
    ) -> frozenset[str] | None:
        """Resource names this workflow can reach from what it declares, or
        None when that cannot be narrowed and everything has to come up.

        A thread body is arbitrary Python, so which locations it visits is not
        knowable before it runs. What every thread does declare is where it
        starts and ends, and a route between two declared positions names the
        movers that carry it. Anything outside that is brought up on first use
        (see `Transporter.ensure_initialized`) rather than brought up at submit
        time: bringing a device up takes its session and resets what it was
        tracking, and a workflow that never touches the arm has no business
        doing that to it.
        """
        if workflow is None:
            return None
        declared: set[str] = set()
        # Auto-spawn templates are declared at build time like any other thread
        # but live in their own registry, not in `thread_templates`.
        for thread in [
            *workflow.thread_templates,
            *workflow.auto_spawn_registry.values(),
        ]:
            declared.add(thread.start_position_id)
            declared.update(thread.end_position_ids)
        if not declared:
            return None
        # A thread may name a device rather than a site, and a device is not a
        # routing node; the journey resolver is what turns either into one.
        names: set[str] = set()
        positions: set[str] = set()
        for declared_id in declared:
            try:
                location = self._system_map.resolve_journey_location(declared_id)
            except KeyError:
                # A position the map cannot resolve: refuse to narrow rather
                # than leave something the run needs uninitialized.
                return None
            # A deck site's `resource` is its own bridge, not the instrument;
            # the mutex key is what names the device that owns it.
            names.add(location.owner_mutex_id or location.resource.name)
            positions.add(location.position_id)
        for source in positions:
            for target in positions:
                if source == target:
                    continue
                names.update(self._system_map.movers_between(source, target))
        return frozenset(names)

    async def _initialize_fresh_worlds(
        self, in_play: frozenset[str] | None = None,
    ) -> None:
        """Initialize each device's resolved world exactly once.

        PURE_SIM-resolved worlds are never initialized: sim drivers need no
        bring-up (pure-sim deployments have always run without one), and
        under a PURE_SIM submission every device resolves sim because
        overrides only ratchet toward sim. A wire world is keyed by mode
        because the device bridge swaps backends per stamped mode: DEVICE_SIM
        and LIVE are different instruments even though both dispatch through
        the local live driver. Successes are marked per world, so a retry
        after a partial failure only re-initializes the devices that failed
        (initialize takes the device's session and resets its tracking).

        `in_play` holds back MOVERS the workflow does not declare, and only
        movers. A transporter is only ever reached through a pick, so
        `Transporter.ensure_initialized` can bring one up on demand if a thread
        body routes somewhere undeclared, which leaves an arm the run never
        touches holding its own session and its own tracking. Neither holds for
        a device: a thread body can put labware on a liquid handler's
        deck without declaring it, and there is no equivalent chokepoint, so
        devices keep coming up whatever the scope.
        """
        pending: list[tuple[tuple[str, WorkflowRunMode], IInitializable]] = []
        for resource in self._resources.resources:
            if not isinstance(resource, IInitializable):
                continue
            if (
                in_play is not None
                and isinstance(resource, TransporterBase)
                and resource.name not in in_play
            ):
                continue
            resolved = (
                resource.effective_mode if isinstance(resource, IModeAware)
                else current_run_mode.get()
            )
            if resolved is WorkflowRunMode.PURE_SIM:
                continue
            world = (resource.name, resolved)
            if world not in self._initialized_worlds:
                pending.append((world, resource))
        results = await asyncio.gather(
            *(resource.initialize() for _, resource in pending),
            return_exceptions=True,
        )
        failures: list[DeviceInitializationError] = []
        for (world, _), result in zip(pending, results):
            if isinstance(result, BaseException):
                failures.append(DeviceInitializationError(world[0], result))
            else:
                self._initialized_worlds.add(world)
        if failures:
            raise failures[0]

    def forget_bringup(self, device_name: str) -> None:
        """Drop the record that this device's worlds were brought up.

        The device bridge that answered those bring-ups rebuilt its drivers, so
        what succeeded no longer exists. Nothing re-checks a device before each
        command the way a pick re-checks the arm, so the next walk is where its
        recovery has to land, and the scope gate has to let that walk run:
        `_lazy_init_done` short-circuits before anything looks at a world.
        Other devices keep their record, so only this one comes up again.
        """
        remaining = {
            world for world in self._initialized_worlds if world[0] != device_name
        }
        if remaining == self._initialized_worlds:
            return
        self._initialized_worlds = remaining
        self._lazy_init_done.clear()
    async def compare_lh_deck_occupancy(
        self, device_location: Location,
    ) -> DeckComparison | None:
        """Ask the driver what its deck holds and set it beside the ledger.

        Reads only. The ledger stays authoritative whatever comes back; this
        exists so a disagreement can be SEEN before the reconcile writes the
        ledger's answer over the driver's. Returns None when the location is
        not a configured liquid handler, so there is nothing to compare.
        """
        device = self._lh_device_for(device_location)
        if device is None:
            return None
        if await device.resolve_deck_config_async() is None:
            return None
        # Under the device lock, like the reconcile: reading the driver and
        # walking the ledger either side of a move in flight invents a
        # disagreement that was never there.
        async with device.lock.held_for("get_deck_state"):
            state = await device.driver.get_deck_state(GetDeckStateRequest())
            driver_site_by_name = {
                item.name: item.site
                for item in state.labware
                if item.site is not None and not _is_deck_furniture(item)
            }
            mutex_key = (
                device_location.owner_mutex_id or device_location.position_id
            )
            disagreements = self._deck_disagreements(
                device, mutex_key, driver_site_by_name, state.interrupted_move,
            )
        return DeckComparison(
            device_name=device.name,
            driver_deck_empty=not driver_site_by_name,
            disagreements=tuple(disagreements),
            interrupted_move_labware=(
                state.interrupted_move.labware
                if state.interrupted_move is not None else None
            ),
        )

    def _deck_disagreements(
        self,
        device: LiquidHandler,
        mutex_key: str,
        driver_site_by_name: dict[str, str],
        interrupted: InterruptedMove | None,
    ) -> list[DeckReconcileConflict]:
        """One entry per labware the two models cannot both be right about."""
        device_name = device.name
        prefix = f"{mutex_key}/"
        disagreements: list[DeckReconcileConflict] = []
        ledger_names: set[str] = set()

        # First so the driver-side walk below knows the ledger has this plate:
        # otherwise it reads as one the ledger never heard of.
        gripper = device.gripper
        in_flight = gripper.labware if gripper is not None else None
        if gripper is not None and in_flight is not None:
            ledger_names.add(in_flight.name)
            disagreements.append(DeckReconcileConflict(
                device_name=device_name,
                labware_id=in_flight.id,
                labware_name=in_flight.name,
                position_id=gripper.gripper_position_id,
                reason=DeckConflictReason.HELD_BY_GRIPPER,
                driver_site=driver_site_by_name.get(in_flight.name),
            ))

        for site_loc in self._system_map.sites_of(mutex_key):
            labware = site_loc.labware
            if labware is None:
                continue
            ledger_names.add(labware.name)
            ledger_site = site_loc.position_id[len(prefix):]
            driver_site = driver_site_by_name.get(labware.name)
            if driver_site == ledger_site:
                continue
            disagreements.append(DeckReconcileConflict(
                device_name=device_name,
                labware_id=labware.id,
                labware_name=labware.name,
                position_id=site_loc.position_id,
                reason=(
                    DeckConflictReason.MISSING_FROM_DRIVER if driver_site is None
                    else DeckConflictReason.DRIVER_SITE_DIFFERS
                ),
                driver_site=driver_site,
            ))

        # Reported against the site the DRIVER names: it is the only site
        # either side has for this labware.
        for name, site in driver_site_by_name.items():
            if name not in ledger_names:
                disagreements.append(self._unknown_to_ledger(device_name, name, site))

        if interrupted is not None:
            began_at = interrupted.from_site or interrupted.to_site
            disagreements.append(DeckReconcileConflict(
                device_name=device_name,
                labware_id=self._labware_id_or_name(interrupted.labware),
                labware_name=interrupted.labware,
                position_id=f"{prefix}{began_at}",
                reason=DeckConflictReason.INTERRUPTED_MOVE,
                driver_site=interrupted.to_site,
            ))
        return disagreements

    def _unknown_to_ledger(
        self, device_name: str, labware_name: str, driver_site: str,
    ) -> DeckReconcileConflict:
        return DeckReconcileConflict(
            device_name=device_name,
            labware_id=self._labware_id_or_name(labware_name),
            labware_name=labware_name,
            position_id=driver_site,
            reason=DeckConflictReason.UNKNOWN_TO_LEDGER,
            driver_site=driver_site,
        )

    def _labware_id_or_name(self, name: str) -> str:
        """The instance id when the ledger knows this labware, else its name.

        An incident about labware the ledger has never heard of still needs
        something to key on, and the driver's name is all there is.
        """
        for labware in self.labwares:
            if labware.name == name:
                return labware.id
        return name

    async def reconcile_lh_deck_occupancy(self, device_location: Location) -> None:
        """Reconcile a liquid handler's driver deck occupancy to the engine ledger.

        The driver deck is a projection: carriers come from the deck config;
        occupancy is whatever the ledger says sits on this device's deck sites
        (residents and transient, keyed by ``template_name`` exactly as
        ``move_plate`` names them). This replaces the declaration-derived augment
        so a cleared resident is NOT resurrected on reboot -- occupancy follows the
        ledger, not the workflow's REUSE_EXISTING declarations.

        Invariant relied on (covered by the resident-reagent pipetting tests): the
        engine ledger mirrors the driver deck, so a full reconcile reconstructs the
        true state. No-ops for non-LiquidHandler / unconfigured devices.

        Constraint: this is a deck-wide clear+rebuild. Volumes AND tips come from
        the contents ledger, so a pipetted occupant keeps its current volumes and
        a picked rack its remaining tips. There is no fallback to what the
        template declared: that declaration is the ledger's opening entry and
        nothing else, so a half-used rack is never re-pushed as a full one.
        """
        device = self._lh_device_for(device_location)
        if device is None:
            return
        deck_config = await self._deck_world_layout(device)
        if deck_config is None:
            return
        world = (device.name, device.effective_mode)
        placement_by_site: dict[str, tuple[str, int]] = {
            site_name: (carrier_name, site_index)
            for site_name, carrier_name, site_index in enumerate_deck_sites(deck_config)
        }

        mutex_key = device_location.owner_mutex_id or device_location.position_id
        prefix = f"{mutex_key}/"
        # Hold the device lock so the snapshot-then-replace is atomic against
        # move/place/pick and other reconciles (else a stale snapshot drops a plate).
        async with device.lock.held_for("deck reconcile"):
            occupancy: list[DeckResourceConfig] = []
            placed_by_name: dict[str, str] = {}
            for site_loc in self._system_map.sites_of(mutex_key):
                placement = placement_by_site.get(site_loc.position_id[len(prefix):])
                labware = site_loc.labware
                if placement is None:
                    if labware is not None:
                        self._report_vanished_site(
                            device, labware, site_loc.position_id,
                        )
                    continue
                if labware is None:
                    continue
                prior_site = placed_by_name.get(labware.name)
                if prior_site is not None:
                    # Instance names are unique by mint; one name at two sites
                    # means a corrupted ledger, not an operator layout choice.
                    raise DeckLabwareIdentityError(
                        f"reconcile_lh_deck_occupancy: device {device.name} holds "
                        f"labware {labware.name!r} at two sites ({prior_site} and "
                        f"{site_loc.position_id}); the ledger is corrupt."
                    )
                catalog_ref = self._deck_catalog_ref(labware, site_loc.position_id)
                if catalog_ref is None:
                    continue
                carrier_name, site_index = placement
                occupancy.append(DeckResourceConfig(
                    name=labware.name, catalog_ref=catalog_ref,
                    parent_id=carrier_name, site_index=site_index,
                    well_state=await labware.driver_well_state(),
                ))
                placed_by_name[labware.name] = site_loc.position_id
                await self._report_contents_divergence(
                    device, labware, site_loc.position_id,
                )
            held = await self._held_plate_occupancy(
                device, prefix, placement_by_site, occupancy,
            )
            if held is not None:
                occupancy.append(held)
            await device.driver.reconcile_deck_occupancy(
                ReconcileDeckOccupancyRequest(resources=occupancy)
            )
        # A completed reconcile discharges the world's obligation, whoever
        # created it (this call, an earlier failed one, or the lazy walk).
        self._worlds_pending_reconcile.discard(world)

    async def _held_plate_occupancy(
        self,
        device: LiquidHandler,
        prefix: str,
        placement_by_site: dict[str, tuple[str, int]],
        declared: list[DeckResourceConfig],
    ) -> DeckResourceConfig | None:
        """How to declare a plate this device's own gripper is holding, if at all.

        The jaws are at no deck site and a deck addresses labware by site, so
        the only site a projection can name is the one the move began at. That
        is also what the driver's own interrupted-move report names, so the two
        models say the same thing rather than one of them losing the plate: a
        rebuild that dropped it left the next retry naming a plate the driver
        no longer had.

        None when there is no such site to name -- nothing recorded the origin,
        the layout no longer provides it, or the site walk has already declared
        that site or that name. The operator is told, because only they can say
        where the plate really is.
        """
        gripper = device.gripper
        in_flight = gripper.labware if gripper is not None else None
        if gripper is None or in_flight is None:
            return None
        origin = gripper.picked_from_position_id
        site_name = (
            origin[len(prefix):] if origin is not None and origin.startswith(prefix)
            else None
        )
        placement = placement_by_site.get(site_name) if site_name is not None else None
        catalog_ref = self._deck_catalog_ref(in_flight, gripper.gripper_position_id)
        taken = {(entry.parent_id, entry.site_index) for entry in declared}
        if (
            placement is None
            or catalog_ref is None
            or placement in taken
            or in_flight.name in {entry.name for entry in declared}
        ):
            orca_logger.warning(
                "reconcile_lh_deck_occupancy: %s gripper is holding %s, and there is "
                "no deck site to declare it at, so the driver no longer has it. Put "
                "the plate somewhere real with edit-labware-location, or discharge it.",
                device.name, in_flight.name,
            )
            self.notify_deck_reconcile_conflict(DeckReconcileConflict(
                device_name=device.name,
                labware_id=in_flight.id,
                labware_name=in_flight.name,
                position_id=gripper.gripper_position_id,
                reason=DeckConflictReason.HELD_BY_GRIPPER,
            ))
            return None
        carrier_name, site_index = placement
        return DeckResourceConfig(
            name=in_flight.name, catalog_ref=catalog_ref,
            parent_id=carrier_name, site_index=site_index,
            well_state=await in_flight.driver_well_state(),
        )

    async def _report_contents_divergence(
        self, device: LiquidHandler, labware: LabwareInstance, position_id: str,
    ) -> None:
        """Say so when the driver's last report and the record disagree.

        Raised at reconcile rather than on every projection: reconcile is what
        runs after the events that cause it (a boot, a reconnect), and a report
        on every move would be noise nobody reads.
        """
        divergence = await self._labware_contents.divergence(labware.ref)
        if divergence is None:
            return
        orca_logger.warning(
            "contents divergence on %s: %s", device.name, divergence.describe(),
        )
        self.notify_deck_reconcile_conflict(DeckReconcileConflict(
            device_name=device.name,
            labware_id=labware.id,
            labware_name=labware.name,
            position_id=position_id,
            reason=DeckConflictReason.CONTENTS_DIFFER,
            detail=divergence.describe(),
        ))

    def add_deck_reconcile_conflict_listener(
        self, listener: DeckReconcileConflictListener,
    ) -> None:
        self._deck_reconcile_conflict_listeners.append(listener)

    def notify_deck_reconcile_conflict(self, conflict: DeckReconcileConflict) -> None:
        for listener in self._deck_reconcile_conflict_listeners:
            listener(conflict)

    def add_ledger_contradiction_listener(
        self, listener: LedgerContradictionListener,
    ) -> None:
        self._ledger_contradiction_listeners.append(listener)

    def notify_ledger_contradiction(
        self, contradiction: LedgerContradiction,
    ) -> None:
        for listener in self._ledger_contradiction_listeners:
            listener(contradiction)

    async def project_labware_on_lh_decks(
        self, labware: LabwareInstance, *locations: Location,
    ) -> None:
        """Make each liquid-handler deck these locations touch agree about ONE
        labware, and touch nothing else standing on it.

        The narrow counterpart to `reconcile_lh_decks`. Whoever calls this knows
        which labware changed, so the driver hears about that one: correcting a
        tip count on one rack no longer destroys and re-creates every neighbour,
        which on a live instrument is a burst of wire commands about labware
        nobody asked to touch, and which on a partial failure leaves the deck
        half-projected with nothing to roll back to.

        Where it sits comes from the device's slots, not the position ledger: the
        placement chokepoint writes the slots first and the ledger last, so
        mid-placement the slots are the ones telling the truth. A labware no slot
        on that deck holds is taken off the driver.
        """
        projected: set[str] = set()
        for location in locations:
            device = self._lh_device_for(location)
            if device is None or device.name in projected:
                continue
            projected.add(device.name)
            await self._agree_about_one_labware(labware, device, location)

    async def retract_labware_from_lh_decks(self, labware: LabwareInstance) -> None:
        """Take one labware off every liquid-handler deck, in every deck world
        that has been laid out.

        The removal counterpart to `project_labware_on_lh_decks`, and it is not
        told where the labware is on purpose. The projection resolves ONE world
        and an operator write resolves to LIVE, so a plate a PURE_SIM run put
        down was never reached. The engine's own answer to "where is it" is no
        better: a thread that ends retires its labware and frees the slot, so by
        the time an operator discharges it, nothing points at the deck it is
        still standing on.

        Removal is by instance name and only touches a deck that already holds
        that name, so asking every handler costs a read and cannot disturb a
        neighbour.

        Best-effort per handler. A discharge is what an operator reaches for
        WHEN a handler is down, so one that refuses because the read did leaves
        them with the row, the slot and no way out but a restart. What the
        engine knows is corrected either way, and the deck that could not be
        reached is named in the log.
        """
        for device in self.devices:
            if isinstance(device, LiquidHandler):
                await self._retract_best_effort(
                    device, labware, device.retract_deck_labware_from_every_world)

    async def retract_labware_from_other_lh_worlds(
        self, labware: LabwareInstance,
    ) -> None:
        """Take one labware off every liquid-handler deck world EXCEPT the
        caller's, which the caller is projecting into itself.

        What a move needs. An operator stating a new position writes it in their
        own world, and the worlds they are not in are left holding the plate at
        the site it has left -- which then refuses the next run that site.
        """
        for device in self.devices:
            if isinstance(device, LiquidHandler):
                await self._retract_best_effort(
                    device, labware, device.retract_deck_labware_from_other_worlds)

    async def _retract_best_effort(
        self,
        device: LiquidHandler,
        labware: LabwareInstance,
        retract: Callable[[LabwareInstance], Awaitable[None]],
    ) -> None:
        try:
            await retract(labware)
        except Exception as exc:
            orca_logger.warning(
                "%s could not be told that %s is gone (%s); its deck may still "
                "hold it. Reconcile that deck once the device answers again.",
                device.name, labware.name, exc,
            )

    async def _agree_about_one_labware(
        self, labware: LabwareInstance, device: LiquidHandler, device_location: Location,
    ) -> None:
        """Push one labware's presence or absence onto one liquid handler's deck.

        Falls back to the deck-wide reconcile when there is no single delta to
        apply: a driver world whose occupancy is still unknown has nothing for a
        delta to land on, and a labware sitting on the device endpoint rather
        than a named deck site has no site to project it at.
        """
        deck_config = await self._deck_world_layout(device)
        if deck_config is None:
            return
        if self._deck_world_owes_a_reconcile(device):
            await self.reconcile_lh_deck_occupancy(device_location)
            return
        site_loc = self._deck_site_holding(device_location, labware)
        if site_loc is None:
            await device.retract_deck_labware(labware)
            return
        site = site_loc.resource
        if not isinstance(site, DeviceDeckSite):
            await self.reconcile_lh_deck_occupancy(device_location)
            return
        if site.driver_site not in {name for name, _, _ in enumerate_deck_sites(deck_config)}:
            self._report_vanished_site(device, labware, site_loc.position_id)
            return
        catalog_ref = self._deck_catalog_ref(labware, site_loc.position_id)
        if catalog_ref is None:
            return
        await device.project_deck_labware(
            labware, at=site.driver_site, catalog_ref=catalog_ref,
            well_state=await labware.driver_well_state(),
        )

    async def _deck_world_layout(
        self, device: LiquidHandler,
    ) -> DeckLayoutConfig | None:
        """The layout this driver world holds, laying its carrier skeleton out
        the first time. None when the device declares no layout.

        One question, one answer: the occupancy a caller then pushes is computed
        from the layout the driver world actually has, so it can never land at
        carriers the driver does not hold.

        A world configured from scratch holds no occupancy, so it owes a
        reconcile; only a completed one discharges that.
        """
        laid_out = await device.deck_world_layout()
        if laid_out is None:
            return None
        deck_config, just_configured = laid_out
        if just_configured:
            self._worlds_pending_reconcile.add((device.name, device.effective_mode))
        return deck_config

    def _deck_world_owes_a_reconcile(self, device: LiquidHandler) -> bool:
        """True while this driver world still owes the occupancy push that
        configuring it created, so a single-labware delta has nothing to land on."""
        return (device.name, device.effective_mode) in self._worlds_pending_reconcile

    def _deck_site_holding(
        self, device_location: Location, labware: LabwareInstance,
    ) -> Location | None:
        """The site of ``device_location``'s device whose slot holds this
        labware, or None."""
        mutex_key = device_location.owner_mutex_id or device_location.position_id
        for site_loc in self._system_map.sites_of(mutex_key):
            if site_loc.labware is labware:
                return site_loc
        return None

    def _deck_catalog_ref(
        self, labware: LabwareInstance, position_id: str,
    ) -> str | None:
        """The catalog reference a deck projection needs, or None when this
        labware cannot be modeled on a driver deck."""
        try:
            template = self.get_labware_template(labware.template_name)
        except KeyError:
            # A persisted resident can outlive its template (renamed or removed
            # in a rebuild); skip + warn so lazy-init never wedges.
            orca_logger.warning(
                "deck projection: labware at %s references unregistered template "
                "%r; skipping (deck cannot model it).",
                position_id, labware.template_name,
            )
            return None
        # labware_type may be a non-str PLR factory callable, not a catalog key;
        # only the string form can seed a deck resource.
        catalog_ref = getattr(template, "labware_type", None)
        if not isinstance(catalog_ref, str):
            return None
        return catalog_ref

    def _report_vanished_site(
        self, device: LiquidHandler, labware: LabwareInstance, position_id: str,
    ) -> None:
        """No automatic answer for a site the layout no longer provides: keep
        the record, tell a human, never guess or drop."""
        orca_logger.warning(
            "deck projection: %s holds %s at %s but the active deck layout no "
            "longer provides that site; the labware cannot be projected to the "
            "driver until an operator relocates or discharges it.",
            device.name, labware.name, position_id,
        )
        self.notify_deck_reconcile_conflict(DeckReconcileConflict(
            device_name=device.name,
            labware_id=labware.id,
            labware_name=labware.name,
            position_id=position_id,
            reason=DeckConflictReason.SITE_NOT_IN_LAYOUT,
        ))

    async def reconcile_lh_decks(self, *locations: Location) -> None:
        """Reconcile each distinct liquid-handler deck these locations sit on.

        A move between two sites of ONE deck is a single reconcile: the
        projection is deck-wide, so a second call would re-send what the first
        just sent, and on hardware that is a real clear-and-rebuild sequence.
        """
        reconciled: set[str] = set()
        for location in locations:
            device = self._lh_device_for(location)
            if device is None or device.name in reconciled:
                continue
            reconciled.add(device.name)
            await self.reconcile_lh_deck_occupancy(location)

    def _lh_device_for(self, location: Location) -> LiquidHandler | None:
        """Resolve the LiquidHandler behind a flat site, a bridge-backed
        single-slot site, or the device's mutex Location; None otherwise."""
        resource = location.resource
        if isinstance(resource, (DeviceDeckSite, LabwareStagingBridge)):
            device = resource.device
        else:
            sites = self._system_map.sites_of(location.position_id)
            first = sites[0].resource if sites else None
            device = (
                first.device
                if isinstance(first, (DeviceDeckSite, LabwareStagingBridge))
                else None
            )
        return device if isinstance(device, LiquidHandler) else None

    def pause_thread(self, thread_id: str) -> None:
        thread = self.get_executing_thread(thread_id)
        thread.request_pause()

    def resume_thread(self, thread_id: str) -> None:
        thread = self.get_executing_thread(thread_id)
        thread.resume_from_manual_pause()

    # --- Skip + insert API (delegates to MutationCoordinator) ---

    def skip_pending_method(
        self, thread_id: str,
        method_id: str | None = None, method_name: str | None = None,
    ) -> None:
        self._mutation_coordinator.skip_method(thread_id, method_id, method_name)

    async def abort_method(
        self, thread_id: str,
        method_id: str | None = None, method_name: str | None = None,
    ) -> None:
        await self._mutation_coordinator.abort_method(thread_id, method_id, method_name)

    def insert_method(
        self, thread_id: str, template: MethodTemplate,
        where: InsertPosition,
    ) -> None:
        self._mutation_coordinator.insert_method(thread_id, template, where)

    async def insert_method_async(
        self, thread_id: str, template: MethodTemplate,
        where: InsertPosition,
    ) -> None:
        await self._mutation_coordinator.insert_method_async(thread_id, template, where)

    async def replace_method(
        self, thread_id: str, target_name: str, template: MethodTemplate,
    ) -> bool:
        return await self._mutation_coordinator.replace_method(thread_id, target_name, template)

    def skip_pending_action(
        self, thread_id: str,
        action_id: str | None = None, action_command: str | None = None,
    ) -> None:
        self._mutation_coordinator.skip_action(thread_id, action_id, action_command)

    def insert_action(
        self, thread_id: str, action_template: ActionTemplate,
        where: InsertPosition,
    ) -> None:
        self._mutation_coordinator.insert_action(thread_id, action_template, where)

    def replace_action(
        self, thread_id: str, target_command: str, action_template: ActionTemplate,
    ) -> bool:
        return self._mutation_coordinator.replace_action(thread_id, target_command, action_template)

    # --- Variable access ---

    @property
    def variable_store(self) -> VariableService:
        return self._variable_store

    def set_variable(self, name: str, value: OptionValue, execution_id: str) -> None:
        self._variable_store.set(name, value, execution_id)

    def get_variable(self, name: str, execution_id: str) -> OptionValue:
        return self._variable_store.resolve(name, execution_id)

    def set_global_variable(self, name: str, value: OptionValue) -> None:
        self._variable_store.set_global(name, value)

    def get_all_variables(self, execution_id: str) -> dict[str, OptionValue]:
        return self._variable_store.get_all(execution_id)

    def load_profile(self, execution_id: str, profile: DeploymentProfile) -> None:
        self._variable_store.load_profile(execution_id, profile)

    def clear_resources(self) -> None:
        return self._resources.clear_resources()


def _is_deck_furniture(item: DeckResourceState) -> bool:
    """True for a fixture the machine was built with, not labware anyone moved.

    ``get_deck_state().labware`` is an occupancy list, so a Flex reports its
    trash and a Hamilton its carriers. The ledger never holds those, and
    without this every compare on such a machine invents a conflict per
    fixture. ``category`` is what tells them apart.
    """
    category = (item.category or "").lower()
    return category == "trash" or category.endswith("carrier")
