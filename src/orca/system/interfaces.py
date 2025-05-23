from types import MappingProxyType


from abc import ABC, abstractmethod

from orca.workflow_models.labware_thread import Method
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow import Workflow
from orca.workflow_models.workflow_templates import WorkflowTemplate


class ISystemInfo(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def version(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        raise NotImplementedError


class IWorkflowTemplateRegistry(ABC):

    @abstractmethod
    def get_workflow_templates(self) -> MappingProxyType[str, WorkflowTemplate]:
        raise NotImplementedError

    @abstractmethod
    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        raise NotImplementedError


class IMethodTemplateRegistry(ABC):

    @abstractmethod
    def get_method_templates(self) -> MappingProxyType[str, MethodTemplate]:
        raise NotImplementedError

    @abstractmethod
    def get_method_template(self, name: str) -> MethodTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_method_template(self, method: MethodTemplate) -> None:
        raise NotImplementedError


class IThreadTemplateRegistry(ABC):
    @abstractmethod
    def get_labware_thread_template(self, name: str) -> ThreadTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_labware_thread_template(self, labware_thread: ThreadTemplate) -> None:
        raise NotImplementedError


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


