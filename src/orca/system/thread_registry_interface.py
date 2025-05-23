from orca.sdk.events.execution_context import WorkflowExecutionContext
from orca.workflow_models.labware_thread import LabwareThread
from orca.workflow_models.thread_template import ThreadTemplate


from abc import ABC, abstractmethod
from typing import List


class IThreadRegistry(ABC):
    @property
    @abstractmethod
    def threads(self) -> List[LabwareThread]:
        raise NotImplementedError

    @abstractmethod
    def get_thread(self, id: str) -> LabwareThread:
        raise NotImplementedError

    @abstractmethod
    def get_thread_by_labware(self, labware_id: str) -> LabwareThread:
        raise NotImplementedError

    @abstractmethod
    def add_thread(self, labware_thread: LabwareThread) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_thread_instance(self, template: ThreadTemplate, context: WorkflowExecutionContext) -> LabwareThread:
        raise NotImplementedError