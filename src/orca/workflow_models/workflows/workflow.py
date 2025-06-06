from abc import ABC, abstractmethod
import uuid
from typing import List

from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.workflow_templates import EventHookInfo, SpawnInfo


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

    def __init__(self, name:str) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._entry_threads: List[LabwareThreadInstance] = []
        self._spawns: List[SpawnInfo] = []
        self._event_hooks: List[EventHookInfo] = []

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def entry_threads(self) -> List[LabwareThreadInstance]:
        return self._entry_threads

    @property
    def spawns(self) -> List[SpawnInfo]:
        return self._spawns
    
    @property
    def event_hooks(self) -> List[EventHookInfo]:
        return self._event_hooks
    
    def add_entry_thread(self, thread: LabwareThreadInstance) -> None:
        self._entry_threads.append(thread)


    def add_spawn(self, spawn: SpawnInfo):
        self._spawns.append(spawn)

    def add_event_hook(self, event_hook: EventHookInfo):
        self._event_hooks.append(event_hook)
    

