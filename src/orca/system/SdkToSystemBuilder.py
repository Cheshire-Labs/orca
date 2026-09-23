from typing import List, Sequence

from orca.resource_models.labware import LabwareTemplate
from orca.state.ops_history import OpsHistory
from orca.resource_models.tracking_context import TrackingContext
from orca.runtime.interfaces import ILabwareCatalog
from orca.runtime.labware_catalog import InMemoryLabwareCatalog
from orca.state.ops_store import IOpsHistoryStore
from orca.events.event_bus import EventBus, SystemBoundEventBus
from orca.events.event_bus_interface import IEventBus
from orca.config import OrcaConfig
from orca.system.system_info import SystemInfo
from orca.system.thread_manager_interface import IThreadManager
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.system.reservation_manager.reservation_manager import ThreadReservationCoordinator
from orca.system.resource_registry import ResourceRegistry
from orca.system.system import System
from orca.system.system_map import SystemMap
from orca.system.thread_manager import ThreadManager
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingThreadFactory, ExecutingThreadRegistry
from orca.workflow_models.labware_threads.residency import build_residency_check
from orca.resource_models.labware_location_service import InMemoryLabwareLocationService
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.workflows.workflow_factories import ThreadFactory
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflowFactory, ExecutingWorkflowRegistry
from orca.workflow_models.workflows.workflow_registry import ExecutingMethodFactory, ExecutingMethodRegistry, MethodRegistry, ThreadRegistry, WorkflowRegistry
from orca.workflow_models.workflows.workflow_factories import MethodFactory, WorkflowFactory
from orca.variables.variable_store import VariableService, VariableStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import ResolvedAcquisition
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.method_template import (
    MethodTemplate,
    drain_pending_method_templates,
)
from orca.workflow_models.thread_template import (
    ThreadTemplate,
    drain_pending_thread_templates,
)
from orca.workflow_models.workflow_templates import WorkflowTemplate


class SdkToSystemBuilder:
    """
    This class is responsible for converting the SDK representation of a system into a system representation.
    """

    def __init__(self,
                 name: str,
                 description: str,
                 labwares: Sequence[LabwareTemplate] | None = None,
                 resources_registry: ResourceRegistry | None = None,
                 system_map: SystemMap | None = None,
                 workflows: List[WorkflowTemplate] | None = None,
                 event_bus: IEventBus | None = None,
                 config: OrcaConfig | None = None,
                 ops_history_store: IOpsHistoryStore | None = None,
                 labware_catalog: ILabwareCatalog | None = None,
                 variable_store: VariableService | None = None,
                 ) -> None:
        workflows = workflows or []
        derived_labwares = self._derive_labwares(labwares, workflows)
        self._derived_labwares = derived_labwares
        self._labware_catalog: ILabwareCatalog = (
            labware_catalog if labware_catalog is not None else self._default_catalog()
        )

        self._config = config or OrcaConfig()
        self._system_info: SystemInfo = SystemInfo(name, description=description, version="1.0.0", model_extra={})
        self._resource_reg: ResourceRegistry = resources_registry or ResourceRegistry()
        self._labware_registry: LabwareRegistry = self._get_labware_registry(derived_labwares)
        self._system_map: SystemMap = system_map or SystemMap(self._resource_reg)
        self._event_bus = SystemBoundEventBus(event_bus or EventBus())

        self._template_registry: TemplateRegistry = self._get_template_registry(workflows)
        self._variable_store: VariableService = (
            variable_store if variable_store is not None
            else VariableService(VariableStore())
        )
        for workflow in workflows:
            if workflow.variable_definitions:
                self._variable_store.register_workflow_definitions(
                    workflow.name, workflow.variable_definitions
                )
        self._method_factory = MethodFactory()
        self._ops_history = OpsHistory(store=ops_history_store)
        self._thread_factory = ThreadFactory(self._method_factory, self._ops_history)
        self._method_registry = MethodRegistry(self._method_factory)
        self._thread_registry = ThreadRegistry(self._thread_factory, 
                                               self._method_registry, 
                                               self._labware_registry)

        workflow_factory = WorkflowFactory(self._thread_factory)
        self._workflow_registry = WorkflowRegistry(workflow_factory, self._thread_registry)

        self._status_manager = StatusManager(self._event_bus)

        self._thread_reservation_coordinator = ThreadReservationCoordinator(
            self._system_map,
            self._thread_registry,
            exclusion_siblings_of=self._system_map.exclusion_siblings_of,
        )
        self._move_hander = MoveHandler(
            self._thread_reservation_coordinator,
            self._system_map,
            self._thread_reservation_coordinator.starvation_registry,
            self._config.reservation,
        )
        # Lazy import: orca.plugins.__init__ imports trackers that pull in
        # entity_resolver -> sdk.build, creating a cycle if imported at module level.
        from orca.plugins.declared_tracking_observer import DeclaredTrackingObserver
        tracking_context = TrackingContext(observer=DeclaredTrackingObserver(), ops_history=self._ops_history)
        self._tracking_context = tracking_context
        method_factory = ExecutingMethodFactory(self._event_bus, self._status_manager, self._variable_store, tracking_context)
        self._executing_method_registry = ExecutingMethodRegistry(self._method_registry, method_factory)
        self._action_resolver = DynamicResourceActionResolver(self._thread_reservation_coordinator, self._system_map, self._config.reservation)
        self._labware_location_service = InMemoryLabwareLocationService()
        residency_check = build_residency_check(self._thread_registry, self._status_manager)
        self._executing_thread_factory = ExecutingThreadFactory(self._event_bus,
                                                                self._move_hander,
                                                                self._status_manager,
                                                                self._thread_reservation_coordinator,
                                                                self._action_resolver,
                                                                self._executing_method_registry,
                                                                self._system_map,
                                                                self._labware_location_service,
                                                                self._config.coordination,
                                                                method_factory=self._method_factory,
                                                                method_registry=self._method_registry,
                                                                variable_store=self._variable_store,
                                                                register_method_template=self._template_registry.add_method_template,
                                                                residency_check=residency_check)
        self._executing_thread_registry = ExecutingThreadRegistry(self._thread_registry,
                                                                  self._executing_thread_factory)
        
        self._thread_manager: IThreadManager = ThreadManager(self._executing_thread_registry)

        thread_factory = self._thread_factory
        thread_registry = self._thread_registry
        async def create_thread_fn(
            template: ThreadTemplate,
            shared_method: ExecutingMethod | None,
            resolved: ResolvedAcquisition | None,
            run_mode: WorkflowRunMode,
        ) -> LabwareThreadInstance:
            thread = await thread_factory.create_instance(
                template, run_mode=run_mode,
                shared_method=shared_method, resolved=resolved,
            )
            thread_registry.add_thread(thread)
            return thread

        executing_workflow_factory = ExecutingWorkflowFactory(self._thread_manager,
                                                              self._thread_reservation_coordinator,
                                                            self._event_bus,
                                                            self._move_hander,
                                                            self._status_manager,
                                                            self._system_map,
                                                            create_thread_fn=create_thread_fn)
        self._executing_workflow_registry = ExecutingWorkflowRegistry(self._workflow_registry, executing_workflow_factory)


    async def bind_labwares(self) -> None:
        """Bind the System catalog onto every derived labware template.

        Split out of `__init__` because `LabwareTemplate.bind_catalog` is
        async (it awaits a catalog read for category validation); a sync
        constructor cannot await it. `build_system` calls this once after
        construction.
        """
        for tmpl in self._derived_labwares:
            await tmpl.bind_catalog(self._labware_catalog)

    @staticmethod
    def _derive_labwares(
        explicit: Sequence[LabwareTemplate] | None,
        workflows: List[WorkflowTemplate],
    ) -> List[LabwareTemplate]:
        # Every labware template needs at least one thread tracking it.
        # The thread is what creates the LabwareInstance and assigns it to
        # actions via `method.assign_thread(input_template, thread.labware)`.
        # An orphan template (in `labwares=` but no thread) cannot satisfy
        # an action's `ctx.labware(name)` lookup at runtime -- the action
        # raises "Labware X not assigned to this action" deep in workflow
        # execution. This build-time check moves the diagnosis up front.
        #
        # The check applies only when the workflow execution model is in
        # play (some thread exists). `StandaloneMethodExecutor` binds
        # labware via labware_start_mapping directly and legitimately
        # passes `labwares=[...]` with no workflow threads.
        threaded_names: set[str] = {
            thread.labware_template.name
            for wf in workflows
            for thread in wf.thread_templates
        }
        if threaded_names and explicit is not None:
            orphans = sorted(
                lw.name for lw in explicit if lw.name not in threaded_names
            )
            if orphans:
                raise ValueError(
                    f"Labware template(s) {orphans!r} registered via "
                    f"`labwares=` but no thread tracks them. Every template "
                    f"needs a thread; for deck-resident labware use a "
                    f"stationary thread with `start=end=<device_location>`."
                )

        seen: dict[str, LabwareTemplate] = {}
        if explicit is not None:
            for lw in explicit:
                seen[lw.name] = lw
        for wf in workflows:
            for thread in wf.thread_templates:
                lt = thread.labware_template
                if lt.name not in seen:
                    seen[lt.name] = lt
        return list(seen.values())

    @staticmethod
    def _default_catalog() -> ILabwareCatalog:
        """Construct the bundled PLR-seeded catalog.

        Used when the caller does not pass `labware_catalog` -- e.g. the
        source-available/sim path, examples, and tests that exercise the SDK directly.
        A hosted production deployment passes its `DbLabwareCatalog`.
        """
        from cheshire_drivers.labware_seed import load_labware_seed
        return InMemoryLabwareCatalog(load_labware_seed())

    def _get_labware_registry(self, labwares: List[LabwareTemplate]) -> LabwareRegistry:
        reg = LabwareRegistry()
        for l in labwares:
            if isinstance(l, LabwareTemplate):
                reg.add_labware_template(l)
        return reg
    
    def _get_template_registry(self, workflows: List[WorkflowTemplate]) -> TemplateRegistry:
        reg = TemplateRegistry()

        for w in workflows:
            reg.add_workflow_template(w)
        for workflow in workflows:
            for thread in workflow.thread_templates:
                reg.add_labware_thread_template(workflow.name, thread)

        # Each workflow's @orca.workflow decorator already drained the
        # module-level pending lists into the workflow's bundled_methods /
        # bundled_threads, so the templates here are workflow-scoped at
        # decoration time. Methods and threads register under
        # (workflow_name, name), so a method or thread named `incubate` in
        # workflow A and another in workflow B coexist. Two workflows
        # referencing the SAME bundled template object is a no-op; two
        # distinct objects sharing a name within ONE workflow raise the
        # matching NameCollisionError (the registry enforces this). A
        # bundled thread already added via thread_templates above is a
        # same-object re-add, so the registry treats it as a no-op.
        for workflow in workflows:
            for m in workflow.bundled_methods:
                if isinstance(m, MethodTemplate):
                    reg.add_method_template(workflow.name, m)
            for t in workflow.bundled_threads:
                reg.add_labware_thread_template(workflow.name, t)

        # Backward compatibility: any decorator that ran outside an
        # @orca.workflow scope (module-level decorations not picked up by
        # the workflow drain) is registered under every workflow's scope so
        # it stays resolvable from any of them. Tests that build
        # methods/threads at module level without a workflow rely on this.
        orphan_methods = drain_pending_method_templates()
        for m in orphan_methods:
            for workflow in workflows:
                reg.add_method_template(workflow.name, m)
        orphan_threads = drain_pending_thread_templates()
        for t in orphan_threads:
            for workflow in workflows:
                reg.add_labware_thread_template(workflow.name, t)

        return reg

    def get_system(self) -> System:
        system = System(self._system_info,
                self._system_map,
                self._resource_reg,
                self._template_registry,
                self._labware_registry,
                self._thread_registry,
                self._executing_method_registry,
                self._executing_thread_registry,
                self._thread_factory,
                self._thread_manager,
                self._method_registry,
                self._workflow_registry,
                self._executing_workflow_registry,
                self._tracking_context,
                self._ops_history,
                self._labware_location_service,
                self._labware_catalog,
                self._variable_store,
                )
        self._event_bus.bind_system(system)

        return system