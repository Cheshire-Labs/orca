from system.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from system.registry_interfaces import IThreadManager
from resource_models.labware import Labware, LabwareTemplate
from system.registry_interfaces import IThreadRegistry, IMethodRegistry, IWorkflowRegistry
from system.system_map import SystemMap
from system.template_registry_interfaces import IThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from workflow_models.workflow import LabwareThread, Method, Workflow
from workflow_models.workflow_templates import ThreadTemplate, MethodTemplate, WorkflowTemplate


from typing import List, Dict, Union


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


class TemplateRegistry(IThreadTemplateRegistry, IWorkflowTemplateRegistry, IMethodTemplateRegistry):
    def __init__(self) -> None:
        self._labware_thread_templates: Dict[str, ThreadTemplate] = {}
        self._method_templates: Dict[str, MethodTemplate] = {}
        self._workflow_templates: Dict[str, WorkflowTemplate] = {}

    def get_labware_thread_template(self, name: str) -> ThreadTemplate:
        return self._labware_thread_templates[name]

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._workflow_templates[name]
    
    def get_method_template(self, name: str) -> MethodTemplate:
        return self._method_templates[name]

    def add_labware_thread_template(self, thread_template: ThreadTemplate) -> None:
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


class ThreadFactory:
    def __init__(self, labware_registry: ILabwareRegistry, system_map: SystemMap) -> None:
        self._labware_registry: ILabwareRegistry = labware_registry
        self._system_map: SystemMap = system_map

    def create_instance(self, template: ThreadTemplate) -> LabwareThread:

        # Instantiate labware
        labware_instance = template.labware_template.create_instance()
        self._labware_registry.add_labware(labware_instance)

        # Build the method sequence

        # TODO: below code comment-disabled 03/30/2024 - relocate the execution of methods

        # method_seq: List[Method] = []
        # for method_resolver in template.method_resolvers:
        #     method = method_resolver.get_instance(self._labware_registry)
        #     method_seq.append(method)

        # create the thread
        thread = LabwareThread(labware_instance.name,
                                labware_instance,
                                template.start_location,
                                template.end_location,
                                self._system_map)

        return thread

class ThreadManager(IThreadManager, IThreadRegistry):
    def __init__(self, labware_registry: ILabwareRegistry, system_map: SystemMap) -> None:
        self._threads: Dict[str, LabwareThread] = {}
        self._has_completed = False
        self._labware_registry = labware_registry
        self._thread_factory = ThreadFactory(labware_registry, system_map)

    @property
    def threads(self) -> List[LabwareThread]:
        return list(self._threads.values())

    def has_completed(self) -> bool:
        return self._has_completed

    def get_thread(self, name: str) -> LabwareThread:
        return self._threads[name]
    
    def add_thread(self, labware_thread: LabwareThread) -> None:
        if labware_thread.name in self._threads.keys():
            raise KeyError(f"Labware Thread {labware_thread.name} is already defined in the system.  Each labware thread must have a unique name")
        self._threads[labware_thread.name] = labware_thread

    def create_thread_instance(self, template: ThreadTemplate) -> LabwareThread:
        thread = self._thread_factory.create_instance(template)
        thread.initialize_labware()
        for method_template in template.method_resolvers:
            method = method_template.get_instance(self._labware_registry)
            thread.append_method_sequence(method)
        self._threads[thread.name] = thread
        return thread

    def execute(self) -> None:
        while any(not thread.has_completed() for thread in self.threads):
            for thread in self.threads:
                thread.execute_next_action()
        self._has_completed = True

class InstanceRegistry(IThreadRegistry, IWorkflowRegistry, IMethodRegistry):
    def __init__(self, labware_reg: LabwareRegistry, system_map: SystemMap) -> None:
        self._labware_registry = labware_reg
        self._system_map = system_map
        self._thread_manager = ThreadManager(self._labware_registry, self._system_map)
        self._workflow_factory = WorkflowFactory(self, self._labware_registry, self._system_map)
        self._workflows: Dict[str, Workflow] = {}
        self._methods: Dict[str, Method] = {}
        
    @property
    def thread_manager(self) -> IThreadManager:
        return self._thread_manager

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
    
    def get_thread(self, name: str) -> LabwareThread:
        return self._thread_manager.get_thread(name)
    
    def add_thread(self, labware_thread: LabwareThread) -> None:
        return self._thread_manager.add_thread(labware_thread)
 
    def create_thread_instance(self, template: ThreadTemplate) -> LabwareThread:
        return self._thread_manager.create_thread_instance(template)
    
    def create_method_instance(self, template: MethodTemplate) -> Method:
        # TODO: Probably needs to be removed from interface
        raise NotImplementedError()
    
    def execute_workflow(self, template: WorkflowTemplate) -> Workflow:
        return self._workflow_factory.create_instance(template)


class WorkflowFactory:
    def __init__(self, labware_thread_reg: IThreadRegistry, labware_registry: ILabwareRegistry, system_map: SystemMap) -> None:
        self._labware_thread_reg: IThreadRegistry = labware_thread_reg
        self._labware_registry: ILabwareRegistry = labware_registry
        self._system_map: SystemMap = system_map

    def create_instance(self, template: WorkflowTemplate) -> Workflow:
        workflow = Workflow(template.name)
        for thread_template in template.start_thread_templates:
            thread = self._labware_thread_reg.create_thread_instance(thread_template)
            workflow.add_start_thread(thread)
        return workflow
