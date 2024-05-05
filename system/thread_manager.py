from typing import List
from system.move_handler import MoveHandler
from system.registry_interfaces import IThreadManager, IThreadRegistry
from system.system_map import SystemMap

import asyncio

from workflow_models.status_enums import LabwareThreadStatus
from workflow_models.workflow import LabwareThread
from workflow_models.workflow_templates import ThreadTemplate

  
class ThreadManager(IThreadManager):
    def __init__(self, 
                 thread_registry: IThreadRegistry,
                 system_map: SystemMap, 
                 move_handler: MoveHandler) -> None:
        self._thread_registry = thread_registry
        self._system_map = system_map
        self._move_handler = move_handler
        self._loop: asyncio.AbstractEventLoop | None = None
    
    @property
    def threads(self) -> List[LabwareThread]:
        return self._thread_registry.threads
    
    def get_thread(self, id: str) -> LabwareThread:
        return self._thread_registry.get_thread(id)
    
    def get_thread_by_labware(self, labware_id: str) -> LabwareThread:
        return self._thread_registry.get_thread_by_labware(labware_id)

    def add_thread(self, labware_thread: LabwareThread) -> None:
        return self._thread_registry.add_thread(labware_thread)

    def create_thread_instance(self, template: ThreadTemplate) -> LabwareThread:
        return self._thread_registry.create_thread_instance(template)

    def has_completed(self) -> bool:
        return all([thread.has_completed() for thread in self._thread_registry.threads])

    @property
    def active_threads(self) -> List[LabwareThread]:
        return [thread for thread in self._thread_registry.threads if not thread.has_completed()]

    @property
    def unstarted_threads(self) -> List[LabwareThread]:
        return [thread for thread in self._thread_registry.threads if thread.status == LabwareThreadStatus.CREATED]

    def execute_all_threads(self) -> None:
        self._loop = asyncio.get_event_loop()
        # self._loop.set_debug(True)
        self._loop.run_until_complete(self.async_execute())

    async def async_execute(self) -> None:
        assert self._loop is not None
        while not self.has_completed():

            for thread in self.active_threads:
                print(f"Thread {thread.name} - {thread.status}")
                if thread in self.unstarted_threads:
                    task = self._loop.create_task(thread.execute_to_complete())
                await asyncio.sleep(0.2)
        print("All threads have completed execution.")
                   