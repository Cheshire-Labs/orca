import typing
from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import ExecutionContext, MethodExecutionContext
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate

if typing.TYPE_CHECKING:
    from orca.system.system_interface import ISystem

class SystemBoundEventHandler(IEventHandler):
    def set_system(self, system: "ISystem") -> None:
        self.system: ISystem = system

    def handle(self, event: str, context: ExecutionContext) -> None:
        raise NotImplementedError("Event handler must implement handle method")
    

class Spawn(SystemBoundEventHandler):
    def __init__(self, spawn_thread: ThreadTemplate, parent_workflow_id: str, parent_method: MethodTemplate, join_method: bool = False) -> None:
        self._spawn_thread = spawn_thread
        self._parent_workflow_id = parent_workflow_id
        self._parent_method = parent_method
        self._join_method = join_method

    def handle(self, event: str, context: ExecutionContext) -> None:
        assert isinstance(context, MethodExecutionContext), "Context must be of type MethodExecutionContext"
        assert context.method_id is not None, "Method ID must be provided in the context for Spawn event handler"
        if context.method_name != self._parent_method.name:
            return

        if event == "METHOD.IN_PROGRESS":
            workflow = self.system.get_executing_workflow(self._parent_workflow_id)
            if self._join_method:
                method = self.system.get_executing_method(context.method_id)
                self._spawn_thread.set_wrapped_method(method)
            thread_instance = self.system.create_and_register_thread_instance(self._spawn_thread)
            workflow.add_and_start_thread(thread_instance)
            # thread = self.system.start_labware_thread(self._spawn_thread)
            # self.system.add_thread(thread)        



class Join(SystemBoundEventHandler):
    def __init__(self, parent_thread: ThreadTemplate, attaching_thread: ThreadTemplate, parent_method: MethodTemplate) -> None:
        self._parent_thread = parent_thread
        self._attaching_thread = attaching_thread
        self._parent_method = parent_method

    def handle(self, event: str, context: ExecutionContext) -> None:
        assert isinstance(context, MethodExecutionContext), "Context must be of type MethodExecutionContext"
        # if context.thread_name != self._parent_thread.name:
        #     return
        if context.method_name != self._parent_method.name:
            return
        if event == "METHOD.IN_PROGRESS":
            if context.method_id is None:
                raise ValueError("Method ID must be provided in the context for Join event handler")
            method = self.system.get_executing_method(context.method_id)
            self._attaching_thread.set_wrapped_method(method)
