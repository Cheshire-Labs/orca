from collections.abc import Callable

from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import ExecutionContext
from orca.system.system_interface import ISystem


class SystemBoundEventHandler(IEventHandler):
    def set_system(self, system: ISystem) -> None:
        self.system: ISystem = system

    def handle(self, event: str, context: ExecutionContext) -> None:
        raise NotImplementedError("Event handler must implement handle method")


class OneExecutionHandler(SystemBoundEventHandler):
    """Passes on to `handler` only the events of one execution."""

    def __init__(self, execution_id: str, handler: IEventHandler | Callable[[str, ExecutionContext], None]) -> None:
        self._execution_id = execution_id
        self._handler = handler

    def set_system(self, system: ISystem) -> None:
        super().set_system(system)
        if isinstance(self._handler, SystemBoundEventHandler):
            self._handler.set_system(system)

    def handle(self, event: str, context: ExecutionContext) -> None:
        if context.execution_id != self._execution_id:
            return
        if isinstance(self._handler, IEventHandler):
            self._handler.handle(event, context)
        else:
            self._handler(event, context)
