from abc import ABC, abstractmethod
from typing import Any, Dict, List
from orca.sdk.event_handler_interface import EventHandler
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.labware_thread import LabwareThread, Method
from orca.workflow_models.status_enums import MethodStatus
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.workflow import Workflow


class ThreadStartContext:
    workflow: Workflow
    thread: LabwareThread

class MethodEventContext:
    workflow: Workflow
    thread: LabwareThread
    method: Method

class IEventBus(ABC):
    @abstractmethod
    def subscribe(self, event_name: str, handler: EventHandler[Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def unsubscribe(self, event_name: str, handler: EventHandler[Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def emit(self, event_name: str, data: Any = None) -> None:
        raise NotImplementedError
    

class EventBus(IEventBus):
    def __init__(self) -> None:
        self._subscribers: Dict[str, List[EventHandler[Any]]] = {}

    def subscribe(self, event_name: str, handler: EventHandler[Any]) -> None:
        if event_name not in self._subscribers:
            self._subscribers[event_name] = []
        self._subscribers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler[Any]) -> None:
        if event_name in self._subscribers:
            self._subscribers[event_name] = [
                h for h in self._subscribers[event_name] if h != handler
            ]
            if not self._subscribers[event_name]:
                del self._subscribers[event_name]

    def emit(self, event_name: str, data: Any = None) -> None:
        for handler in self._subscribers.get(event_name, []):
            handler.handle(event_name, data)



class Spawn(EventHandler[MethodEventContext]):
    def __init__(self, spawn_thread: ThreadTemplate, parent_thread: ThreadTemplate, parent_method: MethodTemplate) -> None:
        self._spawn_thread: ThreadTemplate = spawn_thread
        self._parent_thread: ThreadTemplate = parent_thread
        self._parent_method: MethodTemplate = parent_method
        self._has_executed: bool = False

    def execute(self) -> None:
        assert self.system is not None, "System is not set"
        thread = self.system.create_thread_instance(self._spawn_thread)
        self._has_executed = True

    def handle(self, event: str, context: MethodEventContext) -> None:
        if context.thread.name != self._parent_thread.name:
            return
        if context.method.name != self._parent_method.name:
            return
        if self._has_executed:
            return
        assert self.system is not None, "System is not set"
        if event == MethodStatus.IN_PROGRESS.name.upper():
            thread = self.system.create_thread_instance(self._spawn_thread)
            self._has_executed = True

class Join(EventHandler[MethodEventContext]):
    def __init__(self, parent_thread: ThreadTemplate, attaching_thread: ThreadTemplate, parent_method: MethodTemplate) -> None:
        self._parent_thread: ThreadTemplate = parent_thread
        self._attaching_thread: ThreadTemplate = attaching_thread
        self._parent_method: MethodTemplate = parent_method
        self._has_executed: bool = False

    def handle(self, event: str, context: MethodEventContext) -> None:    
        if context.thread.name != self._parent_thread.name:
            return
        if context.method.name != self._parent_method.name:
            return
        if self._has_executed:
            return
        if event == MethodStatus.IN_PROGRESS.name.upper():
            self._attaching_thread.set_wrapped_method(context.method)
            self._has_executed = True