import asyncio
import logging
from typing import List
from orca.events.execution_context import WorkflowExecutionContext
from orca.system.reservation_manager.errors import IThreadIncidentDeclarer
from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread, ExecutingThreadRegistry, IExecutingThreadRegistry
from orca.resource_models.labware_placement import LabwarePlacer


orca_logger = logging.getLogger("orca")

class ThreadManager(IThreadManager, IExecutingThreadRegistry):
    def __init__(self, thread_registry: ExecutingThreadRegistry) -> None:
        self._thread_registry = thread_registry

    @property
    def executing_threads(self) -> List[ExecutingLabwareThread]:
        return self._thread_registry.threads

    @property
    def threads(self) -> List[ExecutingLabwareThread]:
        return self.executing_threads

    def create_executing_thread(self, thread_id: str, context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        return self._thread_registry.create_executing_thread(thread_id, context)

    def get_executing_thread(self, thread_id: str) -> ExecutingLabwareThread:
        return self._thread_registry.get_executing_thread(thread_id)

    def set_thread_incident_declarer(self, declarer: IThreadIncidentDeclarer) -> None:
        """Forward the deadlock-declarer back-reference to the wrapped registry (S3 R1.5)."""
        self._thread_registry.set_thread_incident_declarer(declarer)

    def set_labware_placer(self, placer: LabwarePlacer) -> None:
        """Forward the placement chokepoint to the wrapped registry."""
        self._thread_registry.set_labware_placer(placer)

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
        for thread in self.unstarted_threads:
            asyncio.get_running_loop().create_task(thread.start())
        seen: set[str] = set()
        while True:
            new = [t for t in self.threads if t.id not in seen]
            if not new:
                break
            seen.update(t.id for t in new)
            await asyncio.gather(*[t.completed.wait() for t in new])
        orca_logger.info("All threads have completed execution.")
