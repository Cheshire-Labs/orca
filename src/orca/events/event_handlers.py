from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import ExecutionContext
from orca.system.system_interface import ISystem


class SystemBoundEventHandler(IEventHandler):
    def set_system(self, system: ISystem) -> None:
        self.system: ISystem = system

    def handle(self, event: str, context: ExecutionContext) -> None:
        raise NotImplementedError("Event handler must implement handle method")
