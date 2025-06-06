from abc import ABC, abstractmethod
import asyncio
import uuid
from typing import Dict, List

from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.labware_thread import ExecutingLabwareThread, LabwareThreadInstance
from orca.workflow_models.workflow_templates import EventHookInfo, SpawnInfo


class WorkflowThreadManager:
    def __init__(self, system_thread_manager: IThreadManager ) -> None:
        self._system_thread_manager: IThreadManager = system_thread_manager
        self._entry_threads: Dict[str, ExecutingLabwareThread] = {}
        self._workflow_threads: Dict[str, ExecutingLabwareThread] = {}
    
    @property
    def entry_threads(self) -> List[ExecutingLabwareThread]:
        return list(self._entry_threads.values())
    
    @property
    def threads(self) -> List[ExecutingLabwareThread]:
        return list(self._workflow_threads.values())
    
    def add_thread(self, thread: ExecutingLabwareThread, is_entry_thread: bool) -> None:
        if is_entry_thread:
            self._entry_threads[thread.id] = thread
        self._workflow_threads[thread.id] = thread
    
    async def start_entry_threads(self) -> None:
        await asyncio.gather(*[thread.start() for thread in self.entry_threads])



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
    

