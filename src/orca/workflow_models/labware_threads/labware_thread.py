from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import Callable, List
from orca.resource_models.location import Location
from orca.resource_models.labware import LabwareInstance
from orca.events.execution_context import ExecutionContext
from orca.workflow_models.interfaces import ILabwareThread, IMethod
from orca.workflow_models.method import MethodInstance

from orca.workflow_models.status_enums import ActionStatus

orca_logger = logging.getLogger("orca")
class IThreadObserver(ABC):
    @abstractmethod
    def thread_notify(self, event: str, thread: LabwareThreadInstance) -> None:
        raise NotImplementedError
    
class ThreadObserver(IThreadObserver):
    def __init__(self, callback: Callable[[str, LabwareThreadInstance], None]) -> None:
        self._callback = callback
    
    def thread_notify(self, event: str, thread: LabwareThreadInstance) -> None:
        self._callback(event, thread) 

class IMethodObserver(ABC):
    @abstractmethod
    def method_notify(self, event: str, method: MethodInstance) -> None:
        raise NotImplementedError

class MethodObserver(IMethodObserver):
    def __init__(self, callback: Callable[[str, MethodInstance], None]) -> None:
        super().__init__()
        self._callback = callback
    
    def method_notify(self, event: str, method: MethodInstance) -> None:
        self._callback(event, method)



class LabwareThreadInstance(ILabwareThread):
    def __init__(self, 
                 labware: LabwareInstance, 
                 start_location: Location, 
                 end_location: Location,
                 ) -> None:
        self._labware: LabwareInstance = labware
        self._start_location: Location = start_location
        self._end_location: Location = end_location
        self._method_sequence: List[IMethod] = []
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
        self._start_location = location
    
    @property
    def end_location(self) -> Location:
        return self._end_location
    
    @end_location.setter
    def end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def labware(self) -> LabwareInstance:
        return self._labware
    
    @property
    def methods(self) -> List[IMethod]:
        return self._method_sequence

    def append_method_sequence(self, method: IMethod) -> None:
        self._method_sequence.append(method)




