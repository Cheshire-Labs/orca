from typing import Dict, List
from orca.system.interfaces import IMethodRegistry, IWorkflowRegistry
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.workflows.workflow_factories import MethodFactory, ThreadFactory
from orca.workflow_models.workflows.workflow_factories import WorkflowFactory
from orca.workflow_models.labware_thread import LabwareThreadInstance
from orca.workflow_models.method import MethodInstance
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate

class MethodRegistry(IMethodRegistry):
    def __init__(self, method_factory: MethodFactory) -> None:
        self._method_factory = method_factory
        self._methods: Dict[str, MethodInstance] = {}

    def get_method(self, id: str) -> MethodInstance:
        return self._methods[id]
    
    def add_method(self, method: MethodInstance) -> None:
        self._methods[method.id] = method

    def create_and_register_method_instance(self, template: MethodTemplate) -> MethodInstance:
        method = self._method_factory.create_instance(template)
        self.add_method(method)
        return method
    
    def clear(self) -> None:
        self._methods.clear()

class ThreadRegistry(IThreadRegistry[LabwareThreadInstance]):
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


