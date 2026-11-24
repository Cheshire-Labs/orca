from abc import ABC, abstractmethod
from types import MappingProxyType
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
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

    @abstractmethod
    def remove_workflow_template(self, name: str) -> WorkflowTemplate | None:
        raise NotImplementedError


class IMethodTemplateRegistry(ABC):

    @abstractmethod
    def get_method_templates(self) -> MappingProxyType[tuple[str, str], MethodTemplate]:
        raise NotImplementedError

    @abstractmethod
    def get_method_template(self, workflow_name: str, name: str) -> MethodTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_method_template(self, workflow_name: str, method: MethodTemplate) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_method_template(self, workflow_name: str, name: str) -> MethodTemplate | None:
        raise NotImplementedError


class IThreadTemplateRegistry(ABC):
    @abstractmethod
    def get_labware_thread_templates(self) -> MappingProxyType[tuple[str, str], ThreadTemplate]:
        raise NotImplementedError

    @abstractmethod
    def get_labware_thread_template(self, workflow_name: str, name: str) -> ThreadTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_labware_thread_template(self, workflow_name: str, labware_thread: ThreadTemplate) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_labware_thread_template(self, workflow_name: str, name: str) -> ThreadTemplate | None:
        raise NotImplementedError
