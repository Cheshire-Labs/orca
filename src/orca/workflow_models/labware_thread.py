from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Callable, List
from orca.resource_models.location import Location
from orca.resource_models.labware import Labware
from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.execution_context import ExecutionContext, MethodExecutionContext, ThreadExecutionContext, WorkflowExecutionContext
from orca.system.move_handler import MoveHandler
from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import SystemMap
from orca.workflow_models.method import ExecutingMethod, Method
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver

from orca.workflow_models.actions.location_action import ExecutingLocationAction, ILocationAction
from orca.workflow_models.actions.move_action import ExecutingMoveAction, MoveAction
from orca.workflow_models.status_enums import MethodStatus, LabwareThreadStatus

orca_logger = logging.getLogger("orca")
class IThreadObserver(ABC):
    @abstractmethod
    def thread_notify(self, event: str, thread: LabwareThread) -> None:
        raise NotImplementedError
    
class ThreadObserver(IThreadObserver):
    def __init__(self, callback: Callable[[str, LabwareThread], None]) -> None:
        self._callback = callback
    
    def thread_notify(self, event: str, thread: LabwareThread) -> None:
        self._callback(event, thread) 

class IMethodObserver(ABC):
    @abstractmethod
    def method_notify(self, event: str, method: Method) -> None:
        raise NotImplementedError

class MethodObserver(IMethodObserver):
    def __init__(self, callback: Callable[[str, Method], None]) -> None:
        super().__init__()
        self._callback = callback
    
    def method_notify(self, event: str, method: Method) -> None:
        self._callback(event, method)



class ILabwareThread(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def start_location(self) -> Location:
        raise NotImplementedError

    @property
    @abstractmethod
    def end_location(self) -> Location:
        raise NotImplementedError

    @property
    @abstractmethod
    def labware(self) -> Labware:
        raise NotImplementedError

    @abstractmethod
    def append_method_sequence(self, method: Method) -> None:
        raise NotImplementedError
    

class LabwareThread(ILabwareThread):
    def __init__(self, 
                 labware: Labware, 
                 start_location: Location, 
                 end_location: Location,
                 ) -> None:
        self._labware: Labware = labware
        self._start_location: Location = start_location
        self._end_location: Location = end_location
        self._method_sequence: List[Method] = []
        # self._status: LabwareThreadStatus = LabwareThreadStatus.UNCREATED
        # self.status = LabwareThreadStatus.CREATED
        # set current_location is after self._status assignment to accommodate scripts changing start location
        # TODO: source of truth needs to be changed to a labware manager
        # self._current_location: Location = self._start_location 

        
    @property
    def id(self) -> str:
        return self._labware.id

    @property
    def name(self) -> str:
        return self._labware.name
    
    @property
    def start_location(self) -> Location:
        return self._start_location
    
    @property
    def end_location(self) -> Location:
        return self._end_location
    
    @end_location.setter
    def end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def labware(self) -> Labware:
        return self._labware
    
    def append_method_sequence(self, method: Method) -> None:
        self._method_sequence.append(method)
    




class ExecutingLabwareThread(ILabwareThread):

    def __init__(self, 
                 thread: LabwareThread,
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
        self._context = context
        self._event_bus = event_bus
        self._action_resolver = DynamicResourceActionResolver(reservation_manager, system_map)
        self._methods: List[ExecutingMethod] = [ExecutingMethod(m, self._event_bus, status_manager, context) for m in thread._method_sequence]
        # set current_location is after self._status assignment to accommodate scripts changing start location
        # TODO: source of truth needs to be changed to a labware manager
        self._current_location: Location = self._thread.start_location 
        self._previous_action: ExecutingLocationAction | None = None
        self._assigned_action: ILocationAction | None = None
        self._move_action: MoveAction | None = None
        self._stop_event = asyncio.Event()
        self.status = LabwareThreadStatus.CREATED
    
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
        return self._end_location
    
    @end_location.setter
    def end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def labware(self) -> Labware:
        return self._thread.labware
    
    def append_method_sequence(self, method: Method) -> None:
        self._thread.append_method_sequence(method)

    def method_notify(self, event: str, method: Method) -> None:
        if event == MethodStatus.COMPLETED.name.upper():
            self._current_method = None

    @property
    def pending_methods(self) -> List[ExecutingMethod]:
        return [m for m in self._methods if not m.has_completed()]
    
    @property
    def completed_methods(self) -> List[ExecutingMethod]:
        return [m for m in self._methods if m.has_completed()]
    
    @property
    def current_method(self) -> ExecutingMethod | None:
        for method in self._methods:
            if method.status == MethodStatus.IN_PROGRESS:
                return method
        return None

    @property
    def assigned_action(self) -> ILocationAction | None:
        return self._assigned_action
        
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

        while not self.has_completed():
            # no methods left, send home
            if len(self.pending_methods) == 0:
                await self._handle_thread_completion()
                return
            
            # needs next action assigned
            if self._assigned_action is None: # or self._assigned_action.status == ActionStatus.COMPLETED:
                await self._handle_action_assignment()

            assert self._assigned_action is not None

            # move to the assigned location
            while self.current_location != self._assigned_action.location and not self._stop_event.is_set():
                await self._handle_thread_move_assignment_to_location_action()
                await asyncio.sleep(0.2)

            if self._stop_event.is_set():
                self._handle_thread_stop()
                return
            
            await self._handle_thread_at_assigned_location()
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        self.status = LabwareThreadStatus.STOPPING
        self._stop_event.set()
        
    def _handle_thread_stop(self) -> None:
        self._stop_event.clear()
        self.status = LabwareThreadStatus.STOPPED
        

    async def _handle_action_assignment(self) -> None:
        assert len(self.pending_methods) > 0
        assert self.current_method is not None, "Current method should not be None when resolving next action"
        self.status = LabwareThreadStatus.RESOLVING_ACTION_LOCATION
        # this is to fix when the lawbare is already at the location 
        # but has not released the reservation of its previous action
        next_step = self.current_method.pending_actions[0]
        if self._previous_action is not None and self._current_location.resource in next_step.resource_pool.resources:
            self._previous_action.release_reservation()
        
        self._assigned_action = await self.current_method.resolve_next_action(self.current_location, self._action_resolver)
        self.status = LabwareThreadStatus.ACTION_LOCATION_RESOLVED

    async def _handle_thread_move_assignment_to_location_action(self) -> None:
        assert self.current_method is not None, "Current method should not be None when resolving next action"
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

        self.status = LabwareThreadStatus.AWAITING_CO_THREADS
        await self.assigned_action.all_labware_is_present.wait()
        self.status = LabwareThreadStatus.EXECUTING_ACTION
        context = MethodExecutionContext(self._context.workflow_id,
                                        self._context.workflow_name, 
                                        self.current_method.id if self.current_method else None,
                                        self.current_method.name if self.current_method else None)
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
