from abc import ABC, abstractmethod
import uuid
from typing import List

from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.workflow_templates import EventHookInfo, WorkflowTemplate


class IWorkflow(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError


class WorkflowInstance(IWorkflow):

    def __init__(
        self,
        name: str,
        template: WorkflowTemplate | None = None,
        id: str | None = None,
    ) -> None:
        # `id` is injected by SystemRuntime so this instance's id equals the
        # execution_id returned from submit_workflow. They are the same concept
        # (one submission = one workflow instance); the external REST API uses
        # execution_id, internal code uses workflow.id, they must be equal.
        # Callers that don't need the tie-in (topology-building, legacy tests)
        # get a fresh UUID.
        self._id = id if id is not None else str(uuid.uuid4())
        self._name = name
        self._template = template
        self._entry_threads: List[LabwareThreadInstance] = []
        self._event_hooks: List[EventHookInfo] = []

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def template(self) -> WorkflowTemplate | None:
        return self._template

    @property
    def entry_threads(self) -> List[LabwareThreadInstance]:
        return self._entry_threads

    @property
    def event_hooks(self) -> List[EventHookInfo]:
        return self._event_hooks
    
    def add_entry_thread(self, thread: LabwareThreadInstance) -> None:
        self._entry_threads.append(thread)


    def add_event_hook(self, event_hook: EventHookInfo):
        self._event_hooks.append(event_hook)
    

