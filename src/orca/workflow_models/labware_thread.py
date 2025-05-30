from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Callable, Dict, List, Tuple
import uuid
from orca.resource_models.location import Location
from orca.resource_models.labware import AnyLabwareTemplate, Labware, LabwareTemplate
from orca.sdk.events.execution_context import ActionExecutionContext, ExecutionContext, MethodExecutionContext, ThreadExecutionContext, WorkflowExecutionContext
from orca.sdk.events.event_bus_interface import IEventBus
from orca.system.move_handler import MoveHandler
from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import SystemMap
from orca.workflow_models.action import LocationActionExecutor, MoveActionExecutor
from orca.workflow_models.dynamic_resource_action import ResourceActionResolver
from orca.workflow_models.status_enums import ActionStatus, MethodStatus, LabwareThreadStatus

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

class StatusManager:
    def __init__(self, event_bus: IEventBus) -> None:
        self._event_bus = event_bus
        self._status_registry: Dict[str, str] = {}

    def get_status(self, entity_id: str) -> str:
        if entity_id not in self._status_registry:
            raise KeyError(f"No status found for entity {entity_id}")
        return self._status_registry[entity_id]
    
    def set_status(self, entity_type: str, entity_id: str, status: str, context: ExecutionContext) -> None:
        if self.get_status(entity_id) == status:
            return
        self._event_bus.emit(f"{entity_type}.{entity_id}.{status}", context)
        self._status_registry[entity_id] = status


class MethodData:
    def __init__(self, name: str) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._actions: List[ResourceActionResolver] = []
        self._status: MethodStatus = MethodStatus.CREATED

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def actions(self) -> List[ResourceActionResolver]:
        return self._actions

    def append_action(self, action: ResourceActionResolver) -> None:
        self._actions.append(action)

class Method:

    def __init__(self, status_manager: StatusManager, method: MethodData, context: ThreadExecutionContext) -> None:
        self._status_manager = status_manager
        self._method = method
        self._current_action: LocationActionExecutor | None = None
        self._context = context
        
    def set_status(self, status: MethodStatus) -> None:
        context = MethodExecutionContext(self._context.workflow_id,
                                        self._context.workflow_name, 
                                        self._context.thread_id, 
                                        self._context.thread_name, 
                                        self._method.id, 
                                        self._method.name)
        self._status_manager.set_status("METHOD", self._method.id, status.name, context)

    def assign_thread(self, input_template: LabwareTemplate, thread: LabwareThread) -> None:
        for step in self._method.actions:
            if input_template in step.expected_input_templates:
                step.assign_input(input_template, thread.labware)
            elif any(isinstance(template, AnyLabwareTemplate) for template in step.expected_input_templates):
                step.assign_input(input_template, thread.labware)
    

    
    @property 
    def pending_steps(self) -> List[ResourceActionResolver]:
        return [step for step in self._actions if step.status != ActionStatus.COMPLETED]

    def has_completed(self) -> bool:
        return self._status == MethodStatus.COMPLETED

    def action_notify(self, event: str, context: ExecutionContext) -> None:
        assert isinstance(context, ActionExecutionContext)
        if "COMPLETED" == context.action_status.upper():
            if len(self.pending_steps) == 0:
                self._set_status(MethodStatus.COMPLETED)
        else:
            self._set_status(MethodStatus.IN_PROGRESS)

    async def resolve_next_action(self, reference_point: Location, reservation_manager: IReservationManager, system_map: SystemMap) -> LocationActionExecutor | None:
        
        if len(self.pending_steps) == 0:
            self._set_status(MethodStatus.COMPLETED)
            return None
        current_step = self.pending_steps[0]

        location_action = await current_step.resolve_action(reference_point, reservation_manager, system_map, MethodExecutionContext(self._context.workflow_id,
                                                                                                     self._context.workflow_name, 
                                                                                                     self._context.thread_id, 
                                                                                                     self._context.thread_name, 
                                                                                                     self._id, 
                                                                                                     self._name)) 
        # TODO: fix this callback
        self._event_bus.subscribe(f"ACTION.{location_action.id}.STATUS_CHANGED", lambda event, context: self.action_notify(event, context))  
        self._set_status(MethodStatus.IN_PROGRESS)        
        return location_action
    
class LabwareThreadData:
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
    
    @start_location.setter
    def start_location(self, location: Location) -> None:
        if self.status != LabwareThreadStatus.UNCREATED and self.status != LabwareThreadStatus.CREATED:
            raise ValueError("Cannot set start location.  Thread is already in progress.")
        self._start_location = location

    @property
    def end_location(self) -> Location:
        return self._end_location
    
    @end_location.setter
    def end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def pending_methods(self) -> List[Method]:
        return [method for method in self._method_sequence if not method.has_completed()]

    def append_method_sequence(self, method: Method) -> None:
        self._method_sequence.append(method)

    def has_completed(self) -> bool:
        return self.status == LabwareThreadStatus.COMPLETED



    

class LabwareThread(IMethodObserver):

    def __init__(self, 
                 thread: LabwareThreadData,
                 move_handler: MoveHandler,
                 status_manager: StatusManager,
                 reservation_manager: IReservationManager, 
                 system_map: SystemMap, 
                 context: WorkflowExecutionContext,
                 ) -> None:
        self._thread = thread
        self._system_map: SystemMap = system_map
        self._method_sequence: List[Method] = []
        # self.__status: LabwareThreadStatus = LabwareThreadStatus.UNCREATED
        self._move_handler = move_handler
        self._reservation_manager = reservation_manager
        self._status_manager = status_manager
        self._context = context
        # self._status = LabwareThreadStatus.CREATED
        # set current_location is after self._status assignment to accommodate scripts changing start location
        # TODO: source of truth needs to be changed to a labware manager
        self._current_location: Location = self._thread.start_location 
        self._previous_action: LocationActionExecutor | None = None
        self._assigned_action: LocationActionExecutor | None = None
        self._move_action: MoveActionExecutor | None = None
        self._stop_event = asyncio.Event()

    def method_notify(self, event: str, method: Method) -> None:
        if event == MethodStatus.COMPLETED.name.upper():
            self._current_method = None

    @property
    def assigned_action(self) -> LocationActionExecutor | None:
        return self._assigned_action
        
    @property
    def move_action(self) -> MoveActionExecutor | None:
        return self._move_action
    
    @property
    def current_location(self) -> Location:
        return self._current_location
    
    def set_current_location(self, location: Location) -> None:
        self._current_location = location
    
    def initialize_labware(self) -> None:
        start_location = self._thread.start_location
        labware = self._thread.labware
        start_location.initialize_labware(labware)

    @property
    def status(self) -> LabwareThreadStatus:
        status = self._status_manager.get_status(self._thread.id)
        return LabwareThreadStatus[status]

    def set_status(self, status: LabwareThreadStatus) -> None:
        if self._thread.status == status:
            return
        context = ThreadExecutionContext(self._context.workflow_id,
                                        self._context.workflow_name, 
                                        self._thread.id, 
                                        self._thread.name)
        self._thread.status = status
        self._status_manager.set_status("THREAD", self._thread.id, status.name, context)

    


    async def start(self) -> None: 
        # initialization check
        if self._thread.status == LabwareThreadStatus.CREATED:
            self.initialize_labware()

        while not self._thread.has_completed():
            # no methods left, send home
            if len(self._thread.pending_methods) == 0:
                await self._handle_thread_completion()
                return
            
            # needs next action assigned
            if self._assigned_action is None: # or self._assigned_action.status == ActionStatus.COMPLETED:
                await self._handle_action_assignment()

            assert self._assigned_action is not None

            # move to the assigned location
            while self.current_location != self._assigned_action.location and not self._stop_event.is_set():
                await self._handle_thread_move_assignment()
                await asyncio.sleep(0.2)

            if self._stop_event.is_set():
                self._handle_thread_stop()
                return
            
            await self._handle_thread_at_assigned_location()
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        self.set_status(LabwareThreadStatus.STOPPING)
        self._stop_event.set()
        
    def _handle_thread_stop(self) -> None:
        self._stop_event.clear()
        self.set_status(LabwareThreadStatus.STOPPED)
        

    async def _handle_action_assignment(self) -> None:
        assert len(self._thread.pending_methods) > 0
        self.set_status(LabwareThreadStatus.AWAITING_ACTION_RESERVATION)
        current_method = self._thread.pending_methods[0]
        # this is to fix when the lawbare is already at the location 
        # but has not released the reservation of its previous action
        current_step = current_method.pending_steps[0]
        if self._previous_action is not None and self._current_location.resource in current_step.resource_pool.resources:
            self._previous_action.release_reservation()
        
        self._assigned_action = await current_method.resolve_next_action(self.current_location, 
                                                                         self._reservation_manager, 
                                                                         self._system_map)

    async def _handle_thread_move_assignment(self) -> None:
        assert self.assigned_action is not None
        self.set_status(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
        current_method = self._thread.pending_methods[0]
        method_execution_context = MethodExecutionContext(self._context.workflow_id,
                                                            self._context.workflow_name, 
                                                            self._thread.id, 
                                                            self._thread.name, 
                                                            current_method.id,
                                                            current_method.name)
        self._move_action = await self._move_handler.resolve_move_action(self._event_bus,
                                                                         method_execution_context,
                                                                         self._thread.labware, 
                                                                   self.current_location, 
                                                                   self.assigned_action.location, 
                                                                   self.assigned_action)
        await self._execute_move_action()

    async def _handle_thread_at_assigned_location(self) -> None:
        assert self.assigned_action is not None
        assert self.current_location == self.assigned_action.location
        # TODO: These action status should probably be asyncio events
        if self.assigned_action.all_input_labware_present():
            self.set_status(LabwareThreadStatus.PERFORMING_ACTION)
            await self.assigned_action.execute()
        else:
            self.set_status(LabwareThreadStatus.AWAITING_CO_THREADS)
            while self.assigned_action.status == ActionStatus.AWAITING_CO_THREADS:
                await asyncio.sleep(0.2)
            self.set_status(LabwareThreadStatus.PERFORMING_ACTION)
        while self.assigned_action.status == ActionStatus.PERFORMING_ACTION:
            await asyncio.sleep(0.2)
        self._previous_action = self._assigned_action
        self._assigned_action = None        
            
       
    async def _handle_thread_completion(self) -> None:
        while self.current_location != self._thread.end_location:  
            self.set_status(LabwareThreadStatus.AWAITING_MOVE_RESERVATION)
            method_execution_context = MethodExecutionContext(self._context.workflow_id,
                                                            self._context.workflow_name, 
                                                            self._thread.id, 
                                                            self._thread.name, 
                                                            None,
                                                            None)          
            self._move_action = await self._move_handler.resolve_move_action(self._event_bus,
                                                                             method_execution_context,
                                                                             self._thread.labware, 
                                                                    self.current_location, 
                                                                    self._thread.end_location, 
                                                                    None)
            await self._execute_move_action()
            await asyncio.sleep(0.2)
        self.set_status(LabwareThreadStatus.COMPLETED)
    
    async def _execute_move_action(self) -> None:
        assert self._move_action is not None
        self.set_status(LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY)
        while self._move_action.target.labware is not None:
            if self._move_action.reservation.deadlocked.is_set():
                await self._handle_deadlock()
            await asyncio.sleep(0.2)
        self.set_status(LabwareThreadStatus.MOVING)
        await self._move_action.execute()
        self.set_current_location(self._move_action.target)
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
