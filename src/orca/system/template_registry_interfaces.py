from orca.workflow_models.workflow_templates import ThreadTemplate, MethodTemplate, WorkflowTemplate


from abc import ABC, abstractmethod


class IThreadTemplateRegistry(ABC):
    @abstractmethod
    def get_labware_thread_template(self, name: str) -> ThreadTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_labware_thread_template(self, labware_thread: ThreadTemplate) -> None:
        raise NotImplementedError


class IMethodTemplateRegistry(ABC):
    @abstractmethod
    def get_method_template(self, name: str) -> MethodTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_method_template(self, method: MethodTemplate) -> None:
        raise NotImplementedError


class IWorkflowTemplateRegistry(ABC):

    @abstractmethod
    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        raise NotImplementedError