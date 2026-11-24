"""The method and workflow registry interfaces."""

from abc import ABC, abstractmethod
from typing import Sequence
from orca.workflow_models.labware_group import LabwareGroup
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.submission import ResolvedAcquisition
from orca.workflow_models.submission_modes import BatchMode
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate


class IMethodRegistry(ABC):
    @abstractmethod
    def get_method(self, id: str) -> IMethod:
        raise NotImplementedError

    @abstractmethod
    def add_method(self, method: IMethod) -> None:
        raise NotImplementedError
    
    
    @abstractmethod
    def create_and_register_method_instance(self, template: MethodTemplate) -> IMethod:
        raise NotImplementedError

class IWorkflowRegistry(ABC):
    @abstractmethod
    def get_workflow(self, id: str) -> WorkflowInstance:
        raise NotImplementedError

    @abstractmethod
    def add_workflow(self, workflow: WorkflowInstance) -> None:
        raise NotImplementedError

    @abstractmethod
    async def create_and_register_workflow_instance(
        self,
        template: WorkflowTemplate,
        submission_id: str | None = None,
        groups: Sequence[LabwareGroup] | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] | None = None,
        id: str | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> WorkflowInstance:
        raise NotImplementedError

    @abstractmethod
    async def build_entry_threads_for(
        self,
        template: WorkflowTemplate,
        submission_id: str,
        groups: Sequence[LabwareGroup],
        batch_mode: BatchMode = BatchMode.STANDALONE,
        resolved_acquisitions: dict[tuple[str, str], ResolvedAcquisition] | None = None,
        *,
        run_mode: WorkflowRunMode,
    ) -> list[LabwareThreadInstance]:
        raise NotImplementedError
