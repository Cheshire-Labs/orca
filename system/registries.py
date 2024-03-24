from resource_models.labware import Labware, LabwareTemplate
from system.registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry, ILabwareThreadRegisty, IMethodRegistry, IWorkflowRegistry
from system.template_registry_interfaces import ILabwareThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from workflow_models.workflow import LabwareThread, Method, Workflow
from workflow_models.workflow_templates import LabwareThreadTemplate, MethodTemplate, WorkflowTemplate


from typing import List, Dict


class LabwareRegistry(ILabwareRegistry, ILabwareTemplateRegistry):
    def __init__(self) -> None:
        self._labwares: Dict[str, Labware] = {}
        self._labware_templates: Dict[str, LabwareTemplate] = {}

    @property
    def labwares(self) -> List[Labware]:
        return list(self._labwares.values())

    def get_labware(self, name: str) -> Labware:
        return self._labwares[name]
    
    def add_labware(self, labware: Labware) -> None:
        self._labwares[labware.name] = labware

    def get_labware_template(self, name: str) -> LabwareTemplate:
        return self._labware_templates[name]

    def add_labware_template(self, labware: LabwareTemplate) -> None:
        self._labware_templates[labware.name] = labware


class TemplateRegistry(ILabwareThreadTemplateRegistry, IWorkflowTemplateRegistry, IMethodTemplateRegistry):
    def __init__(self) -> None:
        self._labware_thread_templates: Dict[str, LabwareThreadTemplate] = {}
        self._method_templates: Dict[str, MethodTemplate] = {}
        self._workflow_templates: Dict[str, WorkflowTemplate] = {}

    def get_labware_thread_template(self, name: str) -> LabwareThreadTemplate:
        return self._labware_thread_templates[name]

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._workflow_templates[name]
    
    def get_method_template(self, name: str) -> MethodTemplate:
        return self._method_templates[name]

    def add_labware_thread_template(self, thread_template: LabwareThreadTemplate) -> None:
        name = thread_template.name
        if name in self._labware_thread_templates.keys():
            raise KeyError(f"Labware {name} is already defined in the system.  Each labware must have a unique name")
        self._labware_thread_templates[name] = thread_template

    def add_method_template(self, method: MethodTemplate) -> None:
        name = method.name
        if name in self._method_templates.keys():
            raise KeyError(f"Method {name} is already defined in the system.  Each method must have a unique name")
        self._method_templates[name] = method

    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        name = workflow.name
        if name in self._workflow_templates.keys():
            raise KeyError(f"Workflow {name} is already defined in the system.  Each workflow must have a unique name")
        self._workflow_templates[name] = workflow


class InstanceRegistry(ILabwareThreadRegisty, IWorkflowRegistry, IMethodRegistry):
    def __init__(self) -> None:
        self._labware_threads: Dict[str, LabwareThread] = {}
        self._workflows: Dict[str, Workflow] = {}
        self._methods: Dict[str, Method] = {}
        
    def get_labware_thread(self, name: str) -> LabwareThread:
        return self._labware_threads[name]
    
    def add_labware_thread(self, labware_thread: LabwareThread) -> None:
        if labware_thread.name in self._labware_threads.keys():
            raise KeyError(f"Labware Thread {labware_thread.name} is already defined in the system.  Each labware thread must have a unique name")
        self._labware_threads[labware_thread.name] = labware_thread

    def get_workflow(self, name: str) -> Workflow:
        return self._workflows[name]
    
    def add_workflow(self, workflow: Workflow) -> None:
        if workflow.name in self._workflows.keys():
            raise KeyError(f"Workflow {workflow.name} is already defined in the system.  Each workflow must have a unique name")
        self._workflows[workflow.name] = workflow
  
    def get_method(self, name: str) -> Method:
        return self._methods[name]
    
    def add_method(self, method: Method) -> None:
        if method.name in self._methods.keys():
            raise KeyError(f"Method {method.name} is already defined in the system.  Each method must have a unique name")
        self._methods[method.name] = method
 
