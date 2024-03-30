from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List
from resource_models.location import Location
from resource_models.labware import Labware
from routing.router import MoveAction, Route
from system.system_map import SystemMap
from workflow_models.action import BaseAction, IActionObserver
from workflow_models.location_action import DynamicResourceAction, LocationAction
from workflow_models.status_enums import ActionStatus, MethodStatus, LabwareThreadStatus



# class IMethod(ABC, IActionObserver):

#     @property
#     @abstractmethod
#     def name(self) -> str:
#         raise NotImplementedError
    
#     @abstractmethod
#     def append_step(self, step: DynamicResourceAction) -> None:
#         raise NotImplementedError
    
#     @abstractmethod
#     def has_completed(self) -> bool:
#         raise NotImplementedError

#     @abstractmethod
#     def resolve_next_action(self, reference_point: Location, system_map: SystemMap) -> LocationAction:
#         raise NotImplementedError
    
#     @abstractmethod
#     def action_notify(self, event: str, action: BaseAction) -> None:
#         raise NotImplementedError
   


# TODO: Recently added, decide to keep or scrap
class IMethodObserver(ABC):
    @abstractmethod
    def method_notify(self, event: str, method: Method) -> None:
        raise NotImplementedError
    
class Method(IActionObserver):

    def __init__(self, name: str) -> None:
        self._name = name
        self._steps: List[DynamicResourceAction] = []
        self._status: MethodStatus = MethodStatus.CREATED
        self._current_step: LocationAction | None = None
        self._observers: List[IMethodObserver] = []

    @property
    def name(self) -> str:
        return self._name
    
    def append_step(self, step: DynamicResourceAction) -> None:
        self._steps.append(step)

    def has_completed(self) -> bool:
        return self._status == MethodStatus.COMPLETED

    def resolve_next_action(self, reference_point: Location, system_map: SystemMap) -> LocationAction:
        if self._current_step is not None and self._current_step.status != ActionStatus.COMPLETED:
            return self._current_step
        
        self._status = MethodStatus.IN_PROGRESS
        if len(self._steps) == 0:
            self._status = MethodStatus.COMPLETED
            raise ValueError("No more steps to execute")
        
        else:
            dynamic_action = self._steps.pop(0)
            self._current_step = dynamic_action.resolve_resource_action(reference_point, system_map)
            self._current_step.add_observer(self)
            return self._current_step

    def action_notify(self, event: str, action: BaseAction) -> None:
        if event == ActionStatus.COMPLETED.name:
            self._current_step = None
            if len(self._steps) == 0:
                self._status = MethodStatus.COMPLETED

    def add_observer(self, observer: IMethodObserver) -> None:
        self._observers.append(observer)



class LabwareThread:

    def __init__(self, name: str, labware: Labware, start_location: Location, end_location: Location, system_map: SystemMap) -> None:
        self._name: str = name
        self._labware: Labware = labware
        self._start_location: Location = start_location
        self._current_location: Location = self._start_location
        self._end_location: Location = end_location
        self._system_map: SystemMap = system_map
        self._method_sequence: List[Method] = []
        self._current_method: Method | None = None
        self._status: LabwareThreadStatus = LabwareThreadStatus.CREATED


    @property
    def name(self) -> str:
        return self._name 
    @property
    def start_location(self) -> Location:
        return self._start_location
    
    @property
    def end_location(self) -> Location:
        return self._end_location
    
    def set_end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def current_location(self) -> Location:
        return self._current_location

    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def pending_methods(self) -> List[Method]:
        return [method for method in self._method_sequence if not method.has_completed()]

    def has_completed(self) -> bool:
        return self._status == LabwareThreadStatus.COMPLETED

    def initialize_labware(self) -> None:
        self._start_location.set_labware(self._labware)

    def append_method_sequence(self, method: Method) -> None:
        self._method_sequence.append(method)

    def execute_next_action(self) -> None:
        if self.has_completed():
            return

        if not self._current_method or self._current_method.has_completed():
            # if there are no more methods to execute, send the labware to the end location
            if len(self.pending_methods) > 0:
                self._current_method = self.pending_methods.pop(0)
            else:
                self._send_labware_to_location(self._end_location)
                self._status = LabwareThreadStatus.COMPLETED
                return
            
        self._status = LabwareThreadStatus.IN_PROGRESS
        next_action = self._current_method.resolve_next_action(self.current_location, self._system_map)
       
        missing_labware = next_action.get_missing_labware()
        if self.labware in missing_labware:
            self._send_labware_to_location(next_action.location)
            missing_labware = next_action.get_missing_labware()

        # if action has all the labware, execute or set as waiting
        if len(missing_labware) == 0:
            next_action.execute()
        else:
            self._status = LabwareThreadStatus.AWAITING_CO_THREADS

                
    def _send_labware_to_location(self, location: Location) -> None:
        if self._current_location == location:
            return
        route: Route = Route(self._current_location, self._system_map)
        route.extend_to_location(location)
        
        for target_location in route.path[1:]:
            transporter = self._system_map.get_transporter_between(self._current_location.teachpoint_name, target_location.teachpoint_name)
            move_action = MoveAction(self._current_location, target_location, transporter)
            move_action.set_labware(self.labware)
            move_action.execute()
            self._current_location = target_location

 
class Workflow:

    def __init__(self, name:str) -> None:
        self._name = name
        self._start_threads: Dict[str, LabwareThread] = {}
        self._threads:  Dict[str, LabwareThread] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def start_threads(self) -> List[LabwareThread]:
        return list(self._start_threads.values())

    def add_start_thread(self, thread: LabwareThread) -> None:
        self._threads[thread.name] = thread
        self._start_threads[thread.name] = thread

    @property
    def threads(self) -> List[LabwareThread]:
        return list(self._threads.values())
    
    def add_thread(self, thread: LabwareThread) -> None:
        self._threads[thread.name] = thread


    def execute(self) -> None:
        for thread in self._threads.values():
            thread.execute_next_action()