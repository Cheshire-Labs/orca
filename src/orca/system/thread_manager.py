import asyncio
import logging
from typing import Dict, List
from orca.system.thread_registry_interface import IThreadRegistry
from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.labware_thread import ExecutingLabwareThread


orca_logger = logging.getLogger("orca")

class ThreadManager(IThreadManager, IThreadRegistry[ExecutingLabwareThread]):
    def __init__(self) -> None:
        self._threads: Dict[str, ExecutingLabwareThread] = {}

    @property
    def threads(self) -> List[ExecutingLabwareThread]:
        return list(self._threads.values())
    
    def add_thread(self, labware_thread: ExecutingLabwareThread) -> None:
        self._threads[labware_thread.id] = labware_thread

    def get_thread(self, id: str) -> ExecutingLabwareThread:
        return self._threads[id]

    def get_thread_by_labware(self, labware_id: str) -> ExecutingLabwareThread:
        matches = list(filter(lambda thread: thread.labware.id == labware_id, self.threads))
        if len(matches) == 0:
            raise KeyError(f"No thread found for labware {labware_id}")
        if len(matches) > 1:
            raise KeyError(f"Multiple threads found for labware {labware_id}")
        return matches[0]


    def has_completed(self) -> bool:
        return all([thread.has_completed() for thread in self.threads])

    @property
    def active_threads(self) -> List[ExecutingLabwareThread]:
        return [thread for thread in self.threads if not thread.has_completed()]

    @property
    def unstarted_threads(self) -> List[ExecutingLabwareThread]:
        return [thread for thread in self.threads if thread.status == LabwareThreadStatus.CREATED]

    async def start_all_threads(self) -> None:
        # # self._loop.set_debug(True)
        # self._loop.run_until_complete(self.async_execute())
        await self.async_execute()

    def stop_all_threads(self) -> None:
        for thread in self.threads:
            thread.stop()

    async def async_execute(self) -> None:
        while not self.has_completed():

            for thread in self.active_threads:
                orca_logger.info(f"Thread {thread.name} - {thread.status}")
                if thread in self.unstarted_threads:
                    loop = asyncio.get_running_loop()
                    loop.create_task(thread.start())
                await asyncio.sleep(0.2)
        orca_logger.info("All threads have completed execution.")
