from abc import ABC, abstractmethod

from workflow_models.workflow import LabwareThread, Method, Workflow
from workflow_models.workflow_templates import ThreadTemplate, MethodTemplate, WorkflowTemplate

class IThreadRegistry(ABC):
    @abstractmethod
    def get_labware_thread(self, name: str) -> LabwareThread:
        raise NotImplementedError

    @abstractmethod
    def add_labware_thread(self, labware_thread: LabwareThread) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_thread_instance(self, template: ThreadTemplate) -> LabwareThread:
        raise NotImplementedError


class IWorkflowRegistry(ABC):
    @abstractmethod
    def get_workflow(self, name: str) -> Workflow:
        raise NotImplementedError
    
    @abstractmethod
    def add_workflow(self, workflow: Workflow) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def create_workflow_instance(self, template: WorkflowTemplate) -> Workflow:
        raise NotImplementedError
    
class IMethodRegistry(ABC):
    @abstractmethod
    def get_method(self, name: str) -> Method:
        raise NotImplementedError
    
    @abstractmethod
    def add_method(self, method: Method) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def create_method_instance(self, template: MethodTemplate) -> Method:
        raise NotImplementedError


class IThreadManager(ABC):
    @abstractmethod
    def add_thread(self, thread: LabwareThread) -> None:
        raise NotImplementedError

