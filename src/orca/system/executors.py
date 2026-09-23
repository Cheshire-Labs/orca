from typing import Dict, List, Optional
import uuid

from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.runtime.run_modes import WorkflowRunMode, current_run_mode
from orca.runtime.sim_diagnostics import (
    maybe_enable_sim_coroutine_diagnostics_from_env,
)
from orca.system.system_interface import ISystem
from orca.system.system_map import ILocationRegistry
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.standalone_method_workflow import (
    build_standalone_method_workflow,
)
from orca.workflow_models.workflow_templates import EventHookInfo, WorkflowTemplate
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow


class WorkflowExecutor:
    """ Executes a workflow template in the context of a system.
    This class is responsible for starting the workflow and managing its execution."""
    def __init__(self, workflow: WorkflowTemplate, system: ISystem) -> None:
        """ Initializes the WorkflowExecutor with a workflow template and a system.
        Args:
            workflow (WorkflowTemplate): The workflow template to be executed.
            system (ISystem): The system in which the workflow will be executed.
        """
        self._workflow_template = workflow
        self._system = system
        self._execution_id: str | None = None

    @property
    def execution_id(self) -> str:
        """Execution id of this run, available after ``start()``."""
        if self._execution_id is None:
            raise RuntimeError("execution_id is available only after start()")
        return self._execution_id

    async def start(self, run_mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM) -> None:
        """Start the execution of the workflow under `run_mode`.

        Seeds the `current_run_mode` ContextVar from `run_mode` so every
        device dispatch beneath this task observes the same mode. The
        workflow instance is also stamped with `run_mode` so per-thread
        snapshots and submission records carry the same value.

        After seeding, `ensure_runtime_initialized` triggers the lazy
        first-thread-touch walk (configure LH decks + initialize fresh
        non-sim device worlds). The walk runs once per run mode; later
        executions under an already-walked mode skip it.

        Awaits entry threads via ``executing_workflow.start()`` and then
        auto-spawned co-labware threads via ``wait_all_threads()``, so the
        call returns only once every thread (including return legs) is done --
        matching what the SystemRuntime submission path awaits.
        """
        current_run_mode.set(run_mode)
        maybe_enable_sim_coroutine_diagnostics_from_env()
        await self._system.ensure_runtime_initialized(self._workflow_template)
        executing_workflow = await self._get_executing_workflow(run_mode)
        await executing_workflow.start()
        await executing_workflow.wait_all_threads()

    async def _get_executing_workflow(
        self, run_mode: WorkflowRunMode,
    ) -> ExecutingWorkflow:
        workflow_instance = await self._system.create_and_register_workflow_instance(
            self._workflow_template, run_mode=run_mode,
        )
        self._execution_id = workflow_instance.id
        self._system.add_workflow(workflow_instance)

        # Wire workflow variable definitions into the variable store
        variable_defs = self._workflow_template.variable_definitions
        if variable_defs:
            self._system.variable_store.register_workflow_definitions(
                self._workflow_template.name, variable_defs
            )
        self._system.variable_store.create_execution(
            workflow_instance.id, self._workflow_template.name
        )

        return self._system.get_executing_workflow(workflow_instance.id)


class StandaloneMethodExecutor:
    """Run one method standalone via the SDK (no SystemRuntime).

    Builds the same synthetic one-method workflow as
    ``SystemRuntime.submit_method`` (via ``build_standalone_method_workflow``):
    the first labware's thread owns the method, the rest converge on it via
    auto-spawn. Runs it through ``WorkflowExecutor``, which awaits every thread
    (entry + spawned return legs) to completion. The runtime path is the
    equivalent under the submission lifecycle.
    """

    def __init__(self,
                 template: MethodTemplate,
                 labware_start_mapping: Dict[LabwareTemplate, str],
                 labware_end_mapping: Dict[LabwareTemplate, str],
                 system: ISystem,
                 name: str | None = None,
                 event_hooks: Optional[List[EventHookInfo]] = None) -> None:
        self._id = str(uuid.uuid4())
        self._method_template = template
        self._name = name or f"{self._method_template.name}_standalone_{self._id}"
        self._system = system
        location_registry: ILocationRegistry = system
        self._start_mapping: Dict[LabwareTemplate, Location] = { template: location_registry.get_location(loc_name) for template, loc_name in labware_start_mapping.items() }
        self._end_mapping: Dict[LabwareTemplate, Location] = { template: location_registry.get_location(loc_name) for template, loc_name in labware_end_mapping.items() }
        self._event_hooks = event_hooks if event_hooks is not None else []
        self._executor: WorkflowExecutor | None = None
        self._validate_labware_location_mappings()

    @property
    def execution_id(self) -> str:
        """Execution id this run's ops_history / tracking records are bucketed under.

        Available only after ``start()`` -- the id is the workflow instance's.
        """
        if self._executor is None:
            raise RuntimeError("execution_id is available only after start()")
        return self._executor.execution_id

    def _validate_labware_location_mappings(self) -> None:
        if not self._start_mapping:
            raise ValueError("start_map must not be empty")
        if not self._end_mapping:
            raise ValueError("end_map must not be empty")

    async def start(self, run_mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM) -> None:
        """Start the standalone method under `run_mode`."""
        threads = [
            (template, self._start_mapping[template], self._end_mapping[template])
            for template in self._start_mapping
        ]
        workflow_template = build_standalone_method_workflow(
            self._name, self._method_template, threads,
        )
        for handler in self._event_hooks:
            workflow_template.add_event_handler(handler.event_name, handler.handler)
        self._executor = WorkflowExecutor(workflow_template, self._system)
        await self._executor.start(run_mode)