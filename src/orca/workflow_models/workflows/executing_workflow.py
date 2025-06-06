from abc import ABC, abstractmethod
from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.event_handlers import Spawn
from orca.sdk.events.execution_context import WorkflowExecutionContext
from orca.system.move_handler import MoveHandler
from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import SystemMap
from orca.system.thread_manager import ThreadManager
from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.status_enums import WorkflowStatus
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.workflows.workflow import IWorkflow, WorkflowInstance


import asyncio
from typing import Dict, List

from orca.workflow_models.workflows.workflow_registry import WorkflowRegistry


class ExecutingWorkflow(IWorkflow):
    def __init__(self,
                 workflow: WorkflowInstance,
                 system_thread_manager: ThreadManager,
                 event_bus: IEventBus,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                reservation_manager: IReservationManager,
                system_map: SystemMap
                 ) -> None:
        self._workflow = workflow
        self._event_bus = event_bus
        self._thread_manager = system_thread_manager
        self._status_manager = status_manager
        self._move_handler = move_handler
        self._reservation_manager = reservation_manager
        self._system_map = system_map
        self._context = WorkflowExecutionContext(self._workflow.id, self._workflow.name)
        self._entry_threads: List[ExecutingLabwareThread] = []
        for entry_thread in self._workflow.entry_threads:
            executing_thread = ExecutingLabwareThread(entry_thread,
                                        self._event_bus,
                                        self._move_handler,
                                        self._status_manager,
                                        self._reservation_manager,
                                        self._system_map,
                                        self._context)
            self._entry_threads.append(executing_thread)
        self._subscribe_events()
        self.status = WorkflowStatus.CREATED

    @property
    def id(self) -> str:
        return self._workflow.id
    
    @property
    def name(self) -> str:
        return self._workflow.name

    @property
    def thread_manager(self) -> ThreadManager:
        return self._thread_manager
    
    @property
    def status(self) -> WorkflowStatus:
        status = self._status_manager.get_status(self._workflow.id)
        return WorkflowStatus[status]

    @status.setter
    def status(self, status: WorkflowStatus) -> None:
        self._status_manager.set_status("WORKFLOW", self._workflow.id, status.name, self._context)

    async def start(self) -> None:
        if self.status != WorkflowStatus.CREATED:
            raise RuntimeError(f"Workflow {self._workflow.name} is already started or completed.")
        await asyncio.gather(*[thread.start() for thread in self._entry_threads])

    def _subscribe_events(self) -> None:

        for spawn in self._workflow.spawns:
            self._event_bus.subscribe("METHOD.IN_PROGRESS", Spawn(spawn.spawn_thread, self.id, spawn.parent_method, spawn.join))

        for event in self._workflow.event_hooks:
            self._event_bus.subscribe(event.event_name, event.handler)

    def add_and_start_thread(self, thread: LabwareThreadInstance) -> None:
        executing_thread = ExecutingLabwareThread(thread,
                            self._event_bus,
                            self._move_handler,
                            self._status_manager, 
                            self._reservation_manager,
                            self._system_map,
                            self._context)

        event_loop = asyncio.get_event_loop()
        event_loop.create_task(executing_thread.start())

    def pause(self) -> None:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError
    

class ExecutingWorkflowFactory:
    def __init__(self, 
                system_thread_manager: ThreadManager, 
                event_bus: IEventBus,
                move_handler: MoveHandler,
                status_manager: StatusManager,
            reservation_manager: IReservationManager,
            system_map: SystemMap
                ) -> None:
        self._system_thread_manager = system_thread_manager
        self._event_bus = event_bus
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._reservation_manager = reservation_manager
        self._system_map = system_map

    def create_instance(self, workflow: WorkflowInstance) -> ExecutingWorkflow:
        return ExecutingWorkflow(workflow,
                 self._system_thread_manager,
                 self._event_bus,
                 self._move_handler,
                 self._status_manager,
                self._reservation_manager,
                self._system_map)
    
class IExecutingWorkflowRegistry(ABC):

    @abstractmethod
    def get_executing_workflow(self, workflow_instance_id: str) -> ExecutingWorkflow:
        raise NotImplementedError
    
class ExecutingWorkflowRegistry(IExecutingWorkflowRegistry):
    def __init__(self, workflow_registry: WorkflowRegistry,  factory: ExecutingWorkflowFactory) -> None:
        self._workflow_registry = workflow_registry
        self._factory = factory
        self._executing_registry: Dict[str, ExecutingWorkflow] = {}
        
    def get_executing_workflow(self, workflow_instance_id: str) -> ExecutingWorkflow:
        if workflow_instance_id in self._executing_registry.keys():
            return self._executing_registry[workflow_instance_id]
        else:
            workflow_instance = self._workflow_registry.get_workflow(workflow_instance_id)
            executing_workflow = self._factory.create_instance(workflow_instance)
            self._executing_registry[executing_workflow.id] = executing_workflow
            return executing_workflow


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
    