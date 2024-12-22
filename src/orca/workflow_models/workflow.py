import uuid
from typing import Dict, List

from orca.system.thread_manager import IThreadManager
from orca.workflow_models.labware_thread import LabwareThread
from orca.workflow_models.workflow_templates import ThreadTemplate


class WorkflowThreadManager:
    def __init__(self, thread_manager: IThreadManager) -> None:
        self._system_thread_manager: IThreadManager = thread_manager
        self._start_threads: Dict[str, LabwareThread] = {}
        self._workflow_thread: Dict[str, LabwareThread] = {}
    
    @property
    def start_threads(self) -> List[LabwareThread]:
        return list(self._start_threads.values())
    
    @property
    def threads(self) -> List[LabwareThread]:
        return list(self._workflow_thread.values())

    def add_start_thread(self, template: ThreadTemplate) -> LabwareThread:
        thread = self.add_thread(template) 
        self._start_threads[thread.id] = thread
        return thread
    
    def add_thread(self, template: ThreadTemplate) -> LabwareThread:
        thread = self._system_thread_manager.create_thread_instance(template)
        self._workflow_thread[thread.id] = thread
        return thread
    
    async def start_all_threads(self) -> None:
        # TODO: change this to only start the start threads
        await self._system_thread_manager.start_all_threads()

class Workflow:

    def __init__(self, name:str, thread_manager: IThreadManager) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._thread_manager: WorkflowThreadManager = WorkflowThreadManager(thread_manager)

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def start_threads(self) -> List[LabwareThread]:
        return self._thread_manager.start_threads
    
    @property
    def threads(self) -> List[LabwareThread]:
        return self._thread_manager.threads

    def add_start_thread(self, template: ThreadTemplate) -> None:
        self._thread_manager.add_start_thread(template)

    def add_thread(self, template: ThreadTemplate) -> None:
        self._thread_manager.add_start_thread(template)

    async def start(self) -> None:
        await self._thread_manager.start_all_threads()

    def pause(self) -> None:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError
    
    def stop(self) -> None:
        raise NotImplementedError