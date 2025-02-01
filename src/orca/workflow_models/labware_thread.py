from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Callable, List
import uuid
from orca.resource_models.location import Location
from orca.resource_models.labware import AnyLabwareTemplate, Labware, LabwareTemplate
from orca.system.move_handler import MoveHandler
from orca.system.reservation_manager import IReservationManager
from orca.system.system_map import SystemMap
from orca.workflow_models.action import BaseAction, IActionObserver, LocationAction, MoveAction
from orca.workflow_models.dynamic_resource_action import DynamicResourceAction
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
    
class Method(IActionObserver):

    def __init__(self, name: str) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._steps: List[DynamicResourceAction] = []
        self._current_action: LocationAction | None = None
        self.__status: MethodStatus = MethodStatus.CREATED
        self._observers: List[IMethodObserver] = []

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def status(self) -> MethodStatus:
        return self._status

    @property
    def _status(self) -> MethodStatus:
        return self.__status

    def _set_status(self, status: MethodStatus) -> None:
        if self.__status == status:
            return
        self.__status = status
        for observer in self._observers:
            observer.method_notify(self._status.name.upper(), self)

    def assign_thread(self, input_template: LabwareTemplate, thread: LabwareThread) -> None:
        for step in self._steps:
            if input_template in step.expected_input_templates:
                step.assign_input(input_template, thread.labware)
            elif any(isinstance(template, AnyLabwareTemplate) for template in step.expected_input_templates):
                step.assign_input(input_template, thread.labware)
    
    def append_step(self, step: DynamicResourceAction) -> None:
        self._steps.append(step)
    
    @property 
    def pending_steps(self) -> List[DynamicResourceAction]:
        return [step for step in self._steps if step.status != ActionStatus.COMPLETED]

    def has_completed(self) -> bool:
        return self._status == MethodStatus.COMPLETED

    def action_notify(self, event: str, action: BaseAction) -> None:
        if event == ActionStatus.COMPLETED.name.upper():
            if len(self.pending_steps) == 0:
                self._set_status(MethodStatus.COMPLETED)
        else:
            self._set_status(MethodStatus.IN_PROGRESS)
        

    def add_observer(self, observer: IMethodObserver) -> None:
        self._observers.append(observer)

    async def resolve_next_action(self, reference_point: Location, reservation_manager: IReservationManager, system_map: SystemMap) -> LocationAction | None:
        
        if len(self.pending_steps) == 0:
            self._set_status(MethodStatus.COMPLETED)
            return None
        current_step = self.pending_steps[0]
        current_step.add_observer(self)
        return await current_step.resolve_action(reference_point, reservation_manager, system_map) 
        

class LabwareThread(IMethodObserver):

    def __init__(self, 
                 labware: Labware, 
                 start_location: Location, 
                 end_location: Location,
                 move_handler: MoveHandler,
                 reservation_manager: IReservationManager, 
                 system_map: SystemMap, 
                 observers: List[IThreadObserver] = []) -> None:
        self._labware: Labware = labware
        self._start_location: Location = start_location
        self._end_location: Location = end_location
        self._system_map: SystemMap = system_map
        self._method_sequence: List[Method] = []
        self.__status: LabwareThreadStatus = LabwareThreadStatus.UNCREATED
        self._observers = observers
        self._move_handler = move_handler
        self._reservation_manager = reservation_manager
        self._status = LabwareThreadStatus.CREATED
        # set current_location is after self._status assignment to accommodate scripts changing start location
        # TODO: source of truth needs to be changed to a labware manager
        self._current_location: Location = self._start_location 
        self._previous_action: LocationAction | None = None
        self._assigned_action: LocationAction | None = None
        self._move_action: MoveAction | None = None
        self._stop_event = asyncio.Event()
        
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
    
    def set_end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def current_location(self) -> Location:
        return self._current_location
    
    def set_current_location(self, location: Location) -> None:
        self._current_location = location

    @property
    def status(self) -> LabwareThreadStatus:
        return self._status

    @property
    def _status(self) -> LabwareThreadStatus:
        return self.__status

    @_status.setter
    def _status(self, status: LabwareThreadStatus) -> None:
        if self.__status == status:
            return
        self.__status = status
        for observer in self._observers:
            observer.thread_notify(self._status.name.upper(), self)

    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def pending_methods(self) -> List[Method]:
        return [method for method in self._method_sequence if not method.has_completed()]

    def has_completed(self) -> bool:
        return self._status == LabwareThreadStatus.COMPLETED

    def initialize_labware(self) -> None:
        self._start_location.initialize_labware(self._labware)

    def append_method_sequence(self, method: Method) -> None:
        self._method_sequence.append(method)
    
    def add_observer(self, observer: IThreadObserver) -> None:
        self._observers.append(observer)

    def method_notify(self, event: str, method: Method) -> None:
        if event == MethodStatus.COMPLETED.name.upper():
            self._current_method = None

    @property
    def assigned_action(self) -> LocationAction | None:
        return self._assigned_action
        
    @property
    def move_action(self) -> MoveAction | None:
        return self._move_action

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
                await self._handle_thread_move_assignment()
                await asyncio.sleep(0.2)

            if self._stop_event.is_set():
                self._handle_thread_stop()
                return
            
            await self._handle_thread_at_assigned_location()
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        self._status = LabwareThreadStatus.STOPPING
        self._stop_event.set()
        
    def _handle_thread_stop(self) -> None:
        self._stop_event.clear()
        self._status = LabwareThreadStatus.STOPPED
        

    async def _handle_action_assignment(self) -> None:
        assert len(self.pending_methods) > 0
        self._status = LabwareThreadStatus.AWAITING_ACTION_RESERVATION
        current_method = self.pending_methods[0]
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
        self._status = LabwareThreadStatus.AWAITING_MOVE_RESERVATION 
        self._move_action = await self._move_handler.resolve_move_action(self._labware, 
                                                                   self.current_location, 
                                                                   self.assigned_action.location, 
                                                                   self.assigned_action)
        await self._execute_move_action()

    async def _handle_thread_at_assigned_location(self) -> None:
        assert self.assigned_action is not None
        assert self.current_location == self.assigned_action.location
        # TODO: These action status should probably be asyncio events
        if self.assigned_action.all_input_labware_present():
            self._status = LabwareThreadStatus.PERFORMING_ACTION
            await self.assigned_action.execute()
        else:
            self._status = LabwareThreadStatus.AWAITING_CO_THREADS
            while self.assigned_action.status == ActionStatus.AWAITING_CO_THREADS:
                await asyncio.sleep(0.2)
            self._status = LabwareThreadStatus.PERFORMING_ACTION
        while self.assigned_action.status == ActionStatus.PERFORMING_ACTION:
            await asyncio.sleep(0.2)
        self._previous_action = self._assigned_action
        self._assigned_action = None        
            
       
    async def _handle_thread_completion(self) -> None:
        while self.current_location != self.end_location:  
            self._status = LabwareThreadStatus.AWAITING_MOVE_RESERVATION           
            self._move_action = await self._move_handler.resolve_move_action(self._labware, 
                                                                    self.current_location, 
                                                                    self.end_location, 
                                                                    None)
            await self._execute_move_action()
            await asyncio.sleep(0.2)
        self._status = LabwareThreadStatus.COMPLETED
    
    async def _execute_move_action(self) -> None:
        assert self._move_action is not None
        self._status = LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY
        while self._move_action.target.labware is not None:
            if self._move_action.reservation.deadlocked.is_set():
                await self._handle_deadlock()
            await asyncio.sleep(0.2)
        self._status = LabwareThreadStatus.MOVING
        await self._move_action.execute()
        self.set_current_location(self._move_action.target)
        # if this was the last labware to pick from the resource, then release the reservation
        if self._previous_action and self._previous_action.all_output_labware_removed():
            self._previous_action.release_reservation()
        self._previous_action = None
        self._move_action = None

    async def _handle_deadlock(self) -> None:
        assert self._move_action is not None
        orca_logger.info(f"Thread {self.name} - Deadlock detected")
        old_target = self._move_action.target
        self._move_action = await self._move_handler.handle_deadlock(self._move_action)
        orca_logger.info(f"Thread {self.name} - Deadlock resolved - reroute target {old_target.name} to {self._move_action.target.name}")
