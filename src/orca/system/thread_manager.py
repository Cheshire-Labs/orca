from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Dict, List
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.move_handler import MoveHandler
from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import SystemMap
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.labware_thread import LabwareThread
from orca.workflow_models.workflow_templates import ThreadTemplate


orca_logger = logging.getLogger("orca")

class ThreadFactory:
    def __init__(self, labware_registry: ILabwareRegistry, move_handler: MoveHandler, reservation_manager: IReservationManager, system_map: SystemMap) -> None:
        self._labware_registry: ILabwareRegistry = labware_registry
        self._system_map: SystemMap = system_map
        self._move_handler = move_handler
        self._reservation_manager = reservation_manager

    def create_instance(self, template: ThreadTemplate) -> LabwareThread:

        # Instantiate labware
        labware_instance = template.labware_template.create_instance()
        self._labware_registry.add_labware(labware_instance)

        # create the thread
        thread = LabwareThread(labware_instance,
                                template.start_location,
                                template.end_location,
                                self._move_handler,
                                self._reservation_manager,
                                self._system_map,
                                template.observers)
        return thread


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
    def create_thread_instance(self, template: ThreadTemplate) -> LabwareThread:
        raise NotImplementedError


class ThreadRegistry(IThreadRegistry):
    def __init__(self, labware_registry: ILabwareRegistry, thread_factory: ThreadFactory) -> None:
        self._threads: Dict[str, LabwareThread] = {}
        self._labware_registry = labware_registry
        self._thread_factory = thread_factory

    @property
    def threads(self) -> List[LabwareThread]:
        return list(self._threads.values())

    def get_thread(self, id: str) -> LabwareThread:
        return self._threads[id]

    def get_thread_by_labware(self, labware_id: str) -> LabwareThread:
        matches = list(filter(lambda thread: thread.labware.id == labware_id, self.threads))
        if len(matches) == 0:
            raise KeyError(f"No thread found for labware {labware_id}")
        if len(matches) > 1:
            raise KeyError(f"Multiple threads found for labware {labware_id}")
        return matches[0]

    def add_thread(self, labware_thread: LabwareThread) -> None:
        if labware_thread.id in self._threads.keys():
            raise KeyError(f"Labware Thread {labware_thread.id} is already defined in the system.  Each labware thread must have a unique id")
        self._threads[labware_thread.id] = labware_thread


    def create_thread_instance(self, template: ThreadTemplate) -> LabwareThread:
        thread = self._thread_factory.create_instance(template)
        thread.initialize_labware()
        for method_template in template.method_resolvers:
            method = method_template.get_instance(self._labware_registry)
            method.assign_thread(template.labware_template, thread)
            thread.append_method_sequence(method)
        self.add_thread(thread)
        return thread


class IThreadManager(IThreadRegistry, ABC):

    @abstractmethod
    async def start_all_threads(self) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def stop_all_threads(self) -> None:
        raise NotImplementedError


class ThreadManager(IThreadManager):
    def __init__(self,
                 thread_registry: IThreadRegistry,
                 system_map: SystemMap,
                 move_handler: MoveHandler) -> None:
        self._thread_registry = thread_registry
        self._system_map = system_map
        self._move_handler = move_handler

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

class ThreadManagerFactory:
    @staticmethod
    def create_instance(labware_registry: ILabwareRegistry, reservation_manager: IReservationManager, system_map: SystemMap, move_handler: MoveHandler) -> IThreadManager:
        thread_factory = ThreadFactory(labware_registry, move_handler, reservation_manager,system_map)
        thread_registry = ThreadRegistry(labware_registry, thread_factory)
        return ThreadManager(thread_registry, system_map, move_handler)