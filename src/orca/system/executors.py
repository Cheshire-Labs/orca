from typing import Optional
import uuid

from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.events.execution_context import WorkflowExecutionContext
from orca.system.interfaces import IMethodRegistry, IWorkflowRegistry
from typing import Dict, List
from orca.system.system_interface import ISystem
from orca.system.system_map import ILocationRegistry
from orca.workflow_models.method_template import JunctionMethodTemplate, MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import EventHookInfo, WorkflowTemplate
from orca.workflow_models.workflows.executing_workflow import IExecutingWorkflowRegistry
from orca.workflow_models.workflows.workflow_registry import IExecutingMethodRegistry


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

    async def start(self, sim: bool = False) -> None:
        """ Starts the execution of the workflow.
        This method creates a workflow instance, registers it with the system, and starts the execution."""
        if sim: 
            self._system.set_simulating(True)
        executing_workflow = self._get_executing_workflow()
        await executing_workflow.start()

    def _get_executing_workflow(self):
        workflow_instance = self._system.create_and_register_workflow_instance(self._workflow_template )
        self._system.add_workflow(workflow_instance)
        return self._system.get_executing_workflow(workflow_instance.id)


class StandalonMethodExecutor:
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
        location_registry:ILocationRegistry = system
        self._start_mapping: Dict[LabwareTemplate, Location] = { template: location_registry.get_location(loc_name) for template, loc_name in labware_start_mapping.items() }
        self._end_mapping: Dict[LabwareTemplate, Location] = { template: location_registry.get_location(loc_name) for template, loc_name in labware_end_mapping.items() }
        self._method_registry: IMethodRegistry = system
        self._workflow_registry: IWorkflowRegistry = system
        self._executing_method_registry: IExecutingMethodRegistry = system
        self._executing_workflow_registry: IExecutingWorkflowRegistry = system
        self._event_hooks = event_hooks if event_hooks is not None else []
        self._validate_labware_location_mappings()
        self._thread_templates = self._get_labware_threads()

    def _validate_labware_location_mappings(self) -> None:
        # simple check that the AnyLabware wildcard is satisfied
        if len(self._start_mapping) != len(self._method_template.inputs):
            raise ValueError(f"Number of labware in the start_map does not match the number of expected inputs")

        if len(self._end_mapping) != len(self._method_template.outputs):
            raise ValueError(f"Number of labware in the end_map does not match the number of expected outputs")

        # validate that each concrete labware template is in the maps
        for labware_template in self._method_template.inputs:
            if isinstance(labware_template, LabwareTemplate) and labware_template not in self._start_mapping.keys():
                raise ValueError(f"Labware {labware_template.name} is expected as an input but its starting location is not in the start_map")

        for labware_template in self._method_template.outputs:
            if isinstance(labware_template, LabwareTemplate) and labware_template not in self._end_mapping.keys():
                raise ValueError(f"Labware {labware_template.name} is expected as an output but its ending location is not in the end_map")

    def _get_labware_threads(self) -> List[ThreadTemplate]:
        # TODO: mappings won't work here for labwares that end or start within a method action      
        method = self._method_registry.create_and_register_method_instance(self._method_template)
        context = WorkflowExecutionContext(
            self._id,
            self._name)
        executing_method = self._executing_method_registry.create_executing_method(method.id, context)
        threads: List[ThreadTemplate] = []
        for idx, labware_template in enumerate(self._start_mapping.keys()):
            thread_template = ThreadTemplate(labware_template,
                                             self._start_mapping[labware_template],
                                             self._end_mapping[labware_template])

            thread_template.add_method(JunctionMethodTemplate())
            thread_template.set_wrapped_method(executing_method)
            threads.append(thread_template)
        return threads

    def _get_workflow_template(self) -> WorkflowTemplate:
        workflow_template = WorkflowTemplate(
            self._name,
            )

        for thread_template in self._thread_templates:
            workflow_template.add_thread(thread_template, True)

        for handler in self._event_hooks:
            workflow_template.add_event_handler(handler.event_name, handler.handler)

        return workflow_template

    async def start(self, sim: bool = False) -> None:
        workflow_template = self._get_workflow_template()
        executor = WorkflowExecutor(workflow_template, self._system)
        await executor.start(sim)