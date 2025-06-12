from typing import Dict, List, Optional
import uuid
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.sdk.events.execution_context import WorkflowExecutionContext
from orca.system.interfaces import IMethodRegistry, IWorkflowRegistry
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.method_template import JunctionMethodTemplate
from orca.workflow_models.workflow_templates import EventHookInfo, WorkflowTemplate
from orca.workflow_models.workflows.executing_workflow import IExecutingWorkflowRegistry
from orca.workflow_models.workflows.workflow_registry import IExecutingMethodRegistry

class NewStandalonMethodExecutor:
    def __init__(self, 
                 template: MethodTemplate, 
                 labware_start_mapping: Dict[LabwareTemplate, Location], 
                 labware_end_mapping: Dict[LabwareTemplate, Location],
                 method_registry: IMethodRegistry,
                 executing_method_registry: IExecutingMethodRegistry,
                 workflow_registry: IWorkflowRegistry,
                 executing_workflow_registry: IExecutingWorkflowRegistry,
                 name: str | None = None,
                 event_hooks: Optional[List[EventHookInfo]] = None) -> None:
        self._id = str(uuid.uuid4())
        self._method_template = template
        self._name = name or f"{self._method_template.name}_standalone_{self._id}"
        self._workflow_registry = workflow_registry
        self._start_mapping = labware_start_mapping
        self._end_mapping = labware_end_mapping
        self._method_registry = method_registry
        self._executing_method_registry = executing_method_registry
        self._executing_workflow_registry = executing_workflow_registry
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
            workflow_template.add_event_hook(handler.event_name, handler.handler)

        return workflow_template
    
    async def start(self) -> None:
        workflow_template = self._get_workflow_template()
        workflow_instance = self._workflow_registry.create_and_register_workflow_instance(workflow_template)
        self._workflow_registry.add_workflow(workflow_instance)
        workflow = self._executing_workflow_registry.get_executing_workflow(workflow_instance.id)
        await workflow.start()    
