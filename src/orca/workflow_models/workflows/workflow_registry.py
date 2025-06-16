from abc import ABC, abstractmethod
from typing import Dict, List
from orca.events.event_bus_interface import IEventBus
from orca.events.execution_context import WorkflowExecutionContext
from orca.system.interfaces import IMethodRegistry, IWorkflowRegistry
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.workflows.workflow_factories import ThreadFactory
from orca.workflow_models.workflows.workflow_factories import MethodFactory
from orca.workflow_models.workflows.workflow_factories import WorkflowFactory
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate

class MethodRegistry(IMethodRegistry):
    def __init__(self, method_factory: MethodFactory) -> None:
        self._method_factory = method_factory
        self._methods: Dict[str, IMethod] = {}

    def get_method(self, id: str) -> IMethod:
        return self._methods[id]
    
    def add_method(self, method: IMethod) -> None:
        self._methods[method.id] = method

    def create_and_register_method_instance(self, template: MethodTemplate) -> IMethod:
        method = self._method_factory.create_instance(template)
        self.add_method(method)
        return method
    
    def clear(self) -> None:
        self._methods.clear()


class ExecutingMethodFactory:
    def __init__(self, event_bus: IEventBus, status_manager: StatusManager) -> None:
        self._event_bus = event_bus
        self._status_manager = status_manager

    def create_instance(self, method: IMethod, context: WorkflowExecutionContext) -> ExecutingMethod:
        executing_method = ExecutingMethod(
            method,
            self._event_bus,
            self._status_manager,
            context
        )
        return executing_method
    
class IExecutingMethodRegistry(ABC):

    @abstractmethod
    def get_executing_method(self, id: str) -> ExecutingMethod:
        raise NotImplementedError("This method should be implemented by subclasses.")
    
    @abstractmethod
    def create_executing_method(self, method_id: str, context: WorkflowExecutionContext) -> ExecutingMethod:
         raise NotImplementedError("This method should be implemented by subclasses.")
    
class ExecutingMethodRegistry(IExecutingMethodRegistry):
    def __init__(self, method_registry: IMethodRegistry, method_factory: ExecutingMethodFactory) -> None:
        self._method_registry = method_registry
        self._method_factory = method_factory
        self._executing_registry: Dict[str, ExecutingMethod] = {}

    def get_executing_method(self, id: str) -> ExecutingMethod:
        return self._executing_registry[id]

    def create_executing_method(self, method_id: str, context: WorkflowExecutionContext) -> ExecutingMethod:
        method = self._method_registry.get_method(method_id)
        executing_method = self._method_factory.create_instance(method, context)
        self._executing_registry[executing_method.id] = executing_method
        return executing_method
    
    def __contains__(self, id: str) -> bool:
        return id in self._executing_registry

class ThreadRegistry(IThreadRegistry):
    def __init__(self,
                 thread_factory: ThreadFactory,
                 method_reg: IMethodRegistry,
                 labware_registry: ILabwareRegistry) -> None:
        self._threads: Dict[str, LabwareThreadInstance] = {}
        self._thread_factory: ThreadFactory = thread_factory
        self._labware_reg = labware_registry
        self._method_reg = method_reg

    @property
    def threads(self) -> List[LabwareThreadInstance]:
        return list(self._threads.values())

    def get_thread(self, id: str) -> LabwareThreadInstance:
        return self._threads[id]

    def get_thread_by_labware(self, labware_id: str) -> LabwareThreadInstance:
        matches = list(filter(lambda thread: thread.labware.id == labware_id, self.threads))
        if len(matches) == 0:
            raise KeyError(f"No thread found for labware {labware_id}")
        if len(matches) > 1:
            raise KeyError(f"Multiple threads found for labware {labware_id}")
        return matches[0]

    def add_thread(self, labware_thread: LabwareThreadInstance) -> None:
        self._threads[labware_thread.id] = labware_thread
        self._labware_reg.add_labware(labware_thread.labware)
        for method in labware_thread.methods:
            self._method_reg.add_method(method)

    def create_and_register_thread_instance(self, template: ThreadTemplate) -> LabwareThreadInstance:
        thread = self._thread_factory.create_instance(template)
        self.add_thread(thread)
        return thread


class WorkflowRegistry(IWorkflowRegistry):
    def __init__(self, workflow_factory: WorkflowFactory, thread_registry: ThreadRegistry) -> None:
        self._workflow_factory = workflow_factory
        self._thread_registry = thread_registry
        self._workflows: Dict[str, WorkflowInstance] = {}

    def get_workflow(self, id: str) -> WorkflowInstance:
        return self._workflows[id]
    
    def add_workflow(self, workflow: WorkflowInstance) -> None:
        self._workflows[workflow.id] = workflow
        for entry_thread in workflow.entry_threads:
            self._thread_registry.add_thread(entry_thread)
    
    def create_and_register_workflow_instance(self, template: WorkflowTemplate) -> WorkflowInstance:
        workflow = self._workflow_factory.create_instance(template)
        self.add_workflow(workflow)
        return workflow
    
    def clear(self) -> None:
        self._workflows.clear()


