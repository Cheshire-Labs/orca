from typing import Callable

from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.event_handler_interface import IEventHandler
from orca.sdk.events.execution_context import ExecutionContext, MethodExecutionContext
from orca.system.system_interface import ISystem
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.status_enums import MethodStatus
from orca.workflow_models.thread_template import ThreadTemplate

class SystemBoundEventHandler(IEventHandler):
    def set_system(self, system: ISystem) -> None:
        self.system: ISystem = system

    def handle(self, event: str, context: ExecutionContext) -> None:
        raise NotImplementedError("Event handler must implement handle method")
    

class Spawn(SystemBoundEventHandler):
    def __init__(self, spawn_thread: ThreadTemplate, parent_thread: ThreadTemplate, parent_method: MethodTemplate) -> None:
        self._spawn_thread: ThreadTemplate = spawn_thread
        self._parent_thread: ThreadTemplate = parent_thread
        self._parent_method: MethodTemplate = parent_method
        self._has_executed: bool = False

    def handle(self, event: str, context: ExecutionContext) -> None:
        assert isinstance(context, MethodExecutionContext), "Context must be of type MethodExecutionContext"
        if context.thread_name != self._parent_thread.name:
            return
        if context.method_name != self._parent_method.name:
            return
        if self._has_executed:
            return
        if event == MethodStatus.IN_PROGRESS.name.upper():
            thread = self.system.create_thread_instance(self._spawn_thread, context)
            self._has_executed = True


class Join(SystemBoundEventHandler):
    def __init__(self, parent_thread: ThreadTemplate, attaching_thread: ThreadTemplate, parent_method: MethodTemplate) -> None:
        self._parent_thread: ThreadTemplate = parent_thread
        self._attaching_thread: ThreadTemplate = attaching_thread
        self._parent_method: MethodTemplate = parent_method
        self._has_executed: bool = False

    def handle(self, event: str, context: ExecutionContext) -> None:
        assert isinstance(context, MethodExecutionContext), "Context must be of type MethodExecutionContext"
        if context.thread_name != self._parent_thread.name:
            return
        if context.method_name != self._parent_method.name:
            return
        if self._has_executed:
            return
        if event == MethodStatus.IN_PROGRESS.name.upper():
            method = self.system.get_method(context.method_id)
            self._attaching_thread.set_wrapped_method(method)
            self._has_executed = True