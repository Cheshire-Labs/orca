from abc import ABC, abstractmethod
from typing import Dict
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.labware_thread import Method
from orca.workflow_models.workflow import Workflow
from orca.workflow_models.workflow_templates import MethodTemplate, WorkflowTemplate
from orca.system.thread_manager import IThreadManager


class WorkflowFactory:
    def __init__(self, thread_manager: IThreadManager, labware_registry: ILabwareRegistry, system_map: SystemMap) -> None:
        self._thread_manager: IThreadManager = thread_manager
        self._labware_registry: ILabwareRegistry = labware_registry
        self._system_map: SystemMap = system_map

    def create_instance(self, template: WorkflowTemplate) -> Workflow:
        workflow = Workflow(template.name, self._thread_manager)
        for thread_template in template.start_thread_templates:
            # thread = self._thread_manager.create_thread_instance(thread_template)  probably not needed
            workflow.add_start_thread(thread_template)
        return workflow


class IWorkflowRegistry(ABC):
    @abstractmethod
    def get_workflow(self, id: str) -> Workflow:
        raise NotImplementedError

    @abstractmethod
    def add_workflow(self, workflow: Workflow) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_workflow_instance(self, template: WorkflowTemplate) -> Workflow:
        raise NotImplementedError

    @abstractmethod
    def get_method(self, id: str) -> Method:
        raise NotImplementedError

    @abstractmethod
    def add_method(self, method: Method) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_method_instance(self, template: MethodTemplate) -> Method:
        raise NotImplementedError


class WorkflowRegistry(IWorkflowRegistry):
    def __init__(self, thread_manager: IThreadManager, labware_registry: ILabwareRegistry, system_map: SystemMap) -> None:
        self._workflow_factory = WorkflowFactory(thread_manager, labware_registry, system_map)
        self._workflows: Dict[str, Workflow] = {}
        self._methods: Dict[str, Method] = {}

    def get_workflow(self, id: str) -> Workflow:
        return self._workflows[id]
    
    def add_workflow(self, workflow: Workflow) -> None:
        if workflow.id in self._workflows.keys():
            raise KeyError(f"Workflow {workflow.id} is already defined in the system.  Each workflow must have a unique id")
        self._workflows[workflow.id] = workflow
    
    def create_workflow_instance(self, template: WorkflowTemplate) -> Workflow:
        return self._workflow_factory.create_instance(template)
    
    def get_method(self, id: str) -> Method:
        return self._methods[id]
    
    def add_method(self, method: Method) -> None:
        if method.id in self._methods.keys():
            raise KeyError(f"Method {method.id} is already defined in the system.  Each method must have a unique id")
        self._methods[method.id] = method

    def create_method_instance(self, template: MethodTemplate) -> Method:
        # TODO: May need to delete this method and rexamine the design for method access
        raise NotImplementedError
    
    def clear(self) -> None:
        self._workflows.clear()
        self._methods.clear()