
from dataclasses import dataclass
from typing import Dict, List, Sequence

from orca.events.event_bus_interface import EventHandlerType
from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware_state import IOverflowStrategy
from orca.variables.variable_definition import VariableDefinition
from orca.variables.variable_store import _validate_workflow_variable_name
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate


@dataclass
class EventHookInfo:
    event_name: str
    handler: EventHandlerType

class WorkflowTemplate:
    """ Creates a template for a workflow. A workflow is a collection of threads and defines their interactions."""
    def __init__(self, name: str) -> None:
        self._name = name
        self._start_threads: Dict[str, ThreadTemplate] = {}
        self._threads: Dict[str, ThreadTemplate] = {}
        self._event_hooks: List[EventHookInfo] = []
        self._variable_definitions: Dict[str, VariableDefinition] = {}
        self._auto_spawn_registry: Dict[str, ThreadTemplate] = {}
        self._capacity_policies: Dict[str, CapacityPolicy] = {}
        self._overflow_strategies: Dict[str, IOverflowStrategy] = {}
        self._transitive_feeders_cache: Dict[str, frozenset[str]] = {}
        # Workflow-scoped drain: @orca.method and @orca.thread decorators
        # invoked between successive @orca.workflow constructions are
        # captured into these lists at @orca.workflow time, so each workflow
        # owns the templates declared inside its build_workflow() closure.
        # Populated by @orca.workflow (orca.orca.workflow). Read by
        # SdkToSystemBuilder when constructing the template registry.
        self._bundled_methods: List["IMethodTemplate"] = []
        self._bundled_threads: List[ThreadTemplate] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def variable_definitions(self) -> Dict[str, VariableDefinition]:
        return self._variable_definitions

    def add_variable(self, name: str, definition: VariableDefinition) -> None:
        """Add a workflow-scoped variable definition. Rejects 'global' or 'global.*' names."""
        _validate_workflow_variable_name(name)
        self._variable_definitions[name] = definition

    @property
    def bundled_methods(self) -> List[IMethodTemplate]:
        """Method templates whose @orca.method decorators ran inside this
        workflow's build_workflow() closure. Populated by @orca.workflow
        when the template is constructed; consumed by SdkToSystemBuilder.
        """
        return list(self._bundled_methods)

    @property
    def bundled_threads(self) -> List[ThreadTemplate]:
        """Thread templates whose @orca.thread decorators ran inside this
        workflow's build_workflow() closure. Same lifecycle as
        bundled_methods.
        """
        return list(self._bundled_threads)

    def attach_bundled_templates(
        self,
        methods: Sequence[IMethodTemplate],
        threads: Sequence[ThreadTemplate],
    ) -> None:
        """Internal: attach decorator-drained templates to this workflow.

        Called by @orca.workflow's decorator after constructing the template
        and draining the module-level pending lists. Idempotent: replaces
        any previously-attached bundle, since each @orca.workflow invocation
        owns its own scope.
        """
        self._bundled_methods = list(methods)
        self._bundled_threads = list(threads)

    @property
    def thread_templates(self) -> List[ThreadTemplate]:
        return list(self._threads.values())

    @property
    def entry_thread_templates(self) -> List[ThreadTemplate]:
        return list(self._start_threads.values())

    @property
    def event_hooks(self) -> List[EventHookInfo]:
        return self._event_hooks

    def add_thread(self, thread: ThreadTemplate, is_start: bool = False) -> None:
        if is_start:
            # Local import: orca.runtime.runtime_interface pulls plugins +
            # events transitively, which would close a cycle through
            # workflow_models. Importing at call time keeps the field-free
            # state at module load.
            if thread.start_reuse_existing:
                from orca.runtime.runtime_interface import ReuseThreadCannotBeEntryError
                raise ReuseThreadCannotBeEntryError(thread.name)
            if thread.immovable:
                from orca.runtime.runtime_interface import ImmovableThreadCannotBeEntryError
                raise ImmovableThreadCannotBeEntryError(thread.name)
        self._threads[thread.name] = thread
        # _threads is the feeder graph; drop the memo so transitive_feeders_for recomputes.
        self._transitive_feeders_cache.clear()
        if is_start:
            self._start_threads[thread.name] = thread

    def add_event_handler(self, event_name: str, handler: EventHandlerType) -> None:
        self._event_hooks.append(EventHookInfo(event_name, handler))

    def register_auto_spawn(self, thread: ThreadTemplate,
                            capacity: CapacityPolicy | None = None,
                            overflow_strategy: IOverflowStrategy | None = None) -> None:
        labware_name = thread.labware_template.name
        if labware_name in self._auto_spawn_registry:
            raise ValueError(
                f"Duplicate auto-spawn registration for labware '{labware_name}': "
                f"'{self._auto_spawn_registry[labware_name].name}' and '{thread.name}'"
            )
        self._auto_spawn_registry[labware_name] = thread
        if capacity is not None:
            self._capacity_policies[labware_name] = capacity
        if overflow_strategy is not None:
            self._overflow_strategies[labware_name] = overflow_strategy

    def get_auto_spawn_template(self, labware_name: str) -> ThreadTemplate | None:
        return self._auto_spawn_registry.get(labware_name)

    def require_auto_spawn_template(self, labware_name: str) -> ThreadTemplate:
        """Strict lookup: raise KeyError with the list of known names if missing.

        Used by internal trusted callers (drain/reroute) where a missing
        template indicates a bug in earlier wiring, not a legitimate miss.
        Raising surfaces the error immediately instead of silently dropping.
        """
        template = self._auto_spawn_registry.get(labware_name)
        if template is None:
            known = sorted(self._auto_spawn_registry.keys())
            raise KeyError(
                f"No auto-spawn template registered for labware '{labware_name}'. "
                f"Known names: {known}"
            )
        return template

    def get_capacity_policy(self, labware_name: str) -> CapacityPolicy | None:
        return self._capacity_policies.get(labware_name)

    def get_overflow_strategy(self, labware_name: str) -> IOverflowStrategy | None:
        return self._overflow_strategies.get(labware_name)

    @property
    def auto_spawn_registry(self) -> Dict[str, ThreadTemplate]:
        return self._auto_spawn_registry

    def feeders_for(self, receiver_template_name: str) -> set[str]:
        """Thread template names that declare ``contributes_to`` this receiver.

        ``receiver_template_name`` is the receiver's LABWARE template name (the
        slot-key segment). Returns every thread whose ``contributes_to`` list
        contains it. Empty set if none declares feeding this receiver; an unfed
        receiver's slot is closed by the worker-quiescence path in
        ``_evaluate_slot_closures`` instead of the feeder path.
        """
        return {
            t.name
            for t in self._threads.values()
            if receiver_template_name in t.contributes_to
        }

    def transitive_feeders_for(self, receiver_labware_name: str) -> frozenset[str]:
        """Labware names that transitively feed this receiver via ``contributes_to``.

        Direct feeders plus each feeder's own feeders, recursively, so a live upstream
        thread that will still PRODUCE a feeder holds the receiver's slot open. Cycle-
        guarded, and excludes the receiver's own labware so a contributes_to cycle
        cannot let a receiver hold its own slot open forever.
        """
        cached = self._transitive_feeders_cache.get(receiver_labware_name)
        if cached is not None:
            return cached
        feeders: set[str] = set()
        seen = {receiver_labware_name}
        pending = [receiver_labware_name]
        while pending:
            for feeder in self.feeders_for(pending.pop()):
                if feeder == receiver_labware_name:
                    continue
                feeders.add(feeder)
                if feeder not in seen:
                    seen.add(feeder)
                    pending.append(feeder)
        frozen = frozenset(feeders)
        self._transitive_feeders_cache[receiver_labware_name] = frozen
        return frozen
