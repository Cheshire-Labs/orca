from abc import ABC, abstractmethod
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.execution_context import ExecutionContext, MethodExecutionContext, ThreadExecutionContext, WorkflowExecutionContext
from orca.system.move_handler import MoveHandler
from orca.system.reservation_manager import IReservationManager, ReservationManager
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver
from orca.workflow_models.actions.location_action import ExecutingLocationAction, ILocationAction
from orca.workflow_models.actions.move_action import ExecutingMoveAction, MoveAction
from orca.workflow_models.interfaces import ILabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance, orca_logger
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.status_enums import LabwareThreadStatus, MethodStatus
from orca.workflow_models.status_manager import StatusManager


import asyncio
from typing import Dict, List

from orca.workflow_models.workflows.workflow_registry import ThreadRegistry


class ExecutingLabwareThread(ILabwareThread):

    def __init__(self,
                 thread: LabwareThreadInstance,
                 event_bus: IEventBus,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                 reservation_manager: IReservationManager,
                 system_map: SystemMap,
                 context: WorkflowExecutionContext,
                 ) -> None:

        self._thread = thread
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._context: WorkflowExecutionContext = context
        self._event_bus = event_bus
        self._action_resolver = DynamicResourceActionResolver(reservation_manager, system_map)
        self._methods: List[ExecutingMethod] = [ExecutingMethod(m, self._event_bus, status_manager, self._context) for m in thread._method_sequence]
        # set current_location is after self._status assignment to accommodate scripts changing start location
        # TODO: source of truth needs to be changed to a labware manager
        self._current_location: Location = self._thread.start_location
        self._previous_action: ExecutingLocationAction | None = None
        # self._assigned_action: ILocationAction | None = None
        self._move_action: MoveAction | None = None
        self._stop_event = asyncio.Event()

    @property
    def id(self) -> str:
        return self._thread.id

    @property
    def name(self) -> str:
        return self._thread.name

    @property
    def start_location(self) -> Location:
        return self._thread.start_location

    @property
    def end_location(self) -> Location:
        return self._thread.end_location

    @end_location.setter
    def end_location(self, location: Location) -> None:
        self._thread.end_location = location

    @property
    def labware(self) -> LabwareInstance:
        return self._thread.labware

    def append_method_sequence(self, method: MethodInstance) -> None:
        self._thread.append_method_sequence(method)

    @property
    def pending_methods(self) -> List[ExecutingMethod]:
        return [m for m in self._methods if not m.has_completed()]

    @property
    def completed_methods(self) -> List[ExecutingMethod]:
        return [m for m in self._methods if m.has_completed()]

    @property
    def assigned_method(self) -> ExecutingMethod | None:
        if len(self.pending_methods) == 0:
            return None
        return self.pending_methods[0]

    @property
    def assigned_action(self) -> ILocationAction | None:
        return self.assigned_method.current_action if self.assigned_method else None

    @property
    def move_action(self) -> MoveAction | None:
        return self._move_action

    @property
    def current_location(self) -> Location:
        return self._current_location

    def update_start_location(self, location: Location) -> None:
        if self.status != LabwareThreadStatus.CREATED:
            raise ValueError("Cannot set start location.  Thread is already in progress.")
        self._start_location = location

    def set_current_location(self, location: Location) -> None:
        self._current_location = location

    @property
    def context(self) -> WorkflowExecutionContext:
        if self._context is None:
            raise ValueError("Context is not set. Call start() before accessing context.")
        return self._context

    @property
    def status(self) -> LabwareThreadStatus:
        status = self._status_manager.get_status(self._thread.id)
        return LabwareThreadStatus[status]

    @status.setter
    def status(self, status: LabwareThreadStatus) -> None:
        context = ThreadExecutionContext(self._context.workflow_id,
                                        self._context.workflow_name,
                                        self._thread.id,
                                        self._thread.name)
        self._status_manager.set_status("THREAD", self._thread.id, status.name, context)


    def has_completed(self) -> bool:
        return self.status == LabwareThreadStatus.COMPLETED

    def initialize_labware(self) -> None:
        start_location = self._thread.start_location
        labware = self._thread.labware
        start_location.initialize_labware(labware)

    async def start(self) -> None:
        # initialization check
        if self.status == LabwareThreadStatus.CREATED:
            self.initialize_labware()

        if self.assigned_action is None:
            await self._start_next_method()

    async def _advance_thread(self) -> None:

        if self._stop_event.is_set():
            self._handle_thread_stop()
            return

        assert self.assigned_action is not None, "Assigned action should not be None when advancing thread"
        while self.current_location != self.assigned_action.location:
            await self._assign_and_execute_move_action_to_location_action()

        await self._handle_thread_at_assigned_location()

    def handle_method_complete(self, event: str, context: ExecutionContext) -> None:
        asyncio.create_task(self._start_next_method())

    def stop(self) -> None:
        self.status = LabwareThreadStatus.STOPPING
        self._stop_event.set()

    def _handle_thread_stop(self) -> None:
        self._stop_event.clear()
        self.status = LabwareThreadStatus.STOPPED

    async def _start_next_method(self) -> None:
        if len(self.pending_methods) == 0:
            orca_logger.info(f"No pending methods for thread {self._thread.name}. Sending thread to end location {self.end_location.name}.")
            await self._handle_thread_completion()
            return
        next_method = self.pending_methods[0]
        orca_logger.info(f"Starting next method: {next_method.name} in thread {self._thread.name}")
        await next_method.resolve_next_action(self.current_location, self._action_resolver)
        self._event_bus.subscribe(f"METHOD.{next_method.id}.{MethodStatus.COMPLETED.name}", self.handle_method_complete)
        await self._advance_thread()

    async def _assign_and_execute_move_action_to_location_action(self) -> None:
        assert self.assigned_action is not None, "Assigned action should not be None when moving to assigned location"
        self.status = LabwareThreadStatus.AWAITING_MOVE_RESERVATION

        self._move_action = await self._move_handler.resolve_move_action(
                                            self._thread.labware,
                                            self.current_location,
                                            self.assigned_action.location,
                                             self.assigned_action)
        await self._execute_move_action()

    async def _handle_thread_at_assigned_location(self) -> None:
        assert self.assigned_action is not None
        assert self.current_location == self.assigned_action.location
        current_method = self.pending_methods[0]
        self.status = LabwareThreadStatus.AWAITING_CO_THREADS
        await self.assigned_action.all_labware_is_present.wait()
        self.status = LabwareThreadStatus.EXECUTING_ACTION
        context = MethodExecutionContext(self._context.workflow_id,
                                        self._context.workflow_name,
                                        current_method.id if current_method else None,
                                        current_method.name if current_method else None)
        executing_action = ExecutingLocationAction(self._status_manager,
                                                   self.assigned_action,
                                                   context)
        await executing_action.execute()

        self._previous_action = executing_action
        self._assigned_action = None


    async def _handle_thread_completion(self) -> None:
        while self.current_location != self._thread.end_location:
            self.status = LabwareThreadStatus.AWAITING_MOVE_RESERVATION
            context = ThreadExecutionContext(self._context.workflow_id,
                                                            self._context.workflow_name,
                                                            self._thread.id,
                                                            self._thread.name)
            self._move_action = await self._move_handler.resolve_move_action(self._thread.labware,
                                                                    self.current_location,
                                                                    self._thread.end_location,
                                                                    None)
            await self._execute_move_action()
            await asyncio.sleep(0.2)
        self.status = LabwareThreadStatus.COMPLETED

    async def _execute_move_action(self) -> None:
        assert self._move_action is not None
        self.status = LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY
        while self._move_action.target.labware is not None:
            if self._move_action.reservation.deadlocked.is_set():
                await self._handle_deadlock()
            await asyncio.sleep(0.2)
        self.status = LabwareThreadStatus.MOVING
        context = ThreadExecutionContext(self._context.workflow_id,
                                        self._context.workflow_name,
                                        self._thread.id,
                                        self._thread.name)
        executing_move_action = ExecutingMoveAction(self._status_manager, context, self._move_action)
        await executing_move_action.execute()
        self.set_current_location(executing_move_action.target)
        # if this was the last labware to pick from the resource, then release the reservation
        if self._previous_action and self._previous_action.all_output_labware_removed():
            self._previous_action.release_reservation()
        self._previous_action = None
        self._move_action = None

    async def _handle_deadlock(self) -> None:
        assert self._move_action is not None
        orca_logger.info(f"Thread {self._thread.name} - Deadlock detected")
        old_target = self._move_action.target
        self._move_action = await self._move_handler.handle_deadlock(self._move_action)
        orca_logger.info(f"Thread {self._thread.name} - Deadlock resolved - reroute target {old_target.name} to {self._move_action.target.name}")


class ExecutingThreadFactory:
    def __init__(self,
                 event_bus: IEventBus,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                 reservation_manager: ReservationManager,
                 system_map: SystemMap) -> None:
        self._event_bus = event_bus
        self._move_handler = move_handler
        self._status_manager = status_manager
        self._reservation_manager = reservation_manager
        self._system_map = system_map

    def create_instance(self,
                        instance: LabwareThreadInstance,
                        context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        return ExecutingLabwareThread(
            instance,
            self._event_bus,
            self._move_handler,
            self._status_manager,
            self._reservation_manager,
            self._system_map,
            context
        )

class IExecutingThreadRegistry(ABC):

    @abstractmethod
    def create_executing_thread(self, thread_id: str, context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        raise NotImplementedError

    @abstractmethod
    def get_executing_thread(self, thread_id: str) -> ExecutingLabwareThread:
        raise NotImplementedError

class ExecutingThreadRegistry(IExecutingThreadRegistry):
    def __init__(self,
                 thread_registry: ThreadRegistry,
                 executing_thread_factory: ExecutingThreadFactory
                 ) -> None:
        self._thread_registry = thread_registry
        self._factory = executing_thread_factory
        self._executing_registry: Dict[str, ExecutingLabwareThread] = {}

    @property
    def threads(self) -> List[ExecutingLabwareThread]:
        return list(self._executing_registry.values())
    
    def create_executing_thread(self, thread_id: str, context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        if thread_id in self._executing_registry:
            return self._executing_registry[thread_id]
        else:
            instance = self._thread_registry.get_thread(thread_id)
            executing_thread = self._factory.create_instance(instance, context)
            self._executing_registry[thread_id] = executing_thread
            executing_thread.status = LabwareThreadStatus.CREATED
            return executing_thread
        

    def get_executing_thread(self, thread_id: str) -> ExecutingLabwareThread:
        if thread_id not in self._executing_registry:
            raise ValueError(f"Thread {thread_id} has not been created yet.")
        return self._executing_registry[thread_id]