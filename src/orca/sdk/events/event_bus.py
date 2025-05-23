from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.event_handler_interface import IEventHandler


from typing import TYPE_CHECKING, Callable, Dict, List

from orca.sdk.events.event_handlers import SystemBoundEventHandler
from orca.sdk.events.execution_context import ExecutionContext

if TYPE_CHECKING:
    from orca.system.system_interface import ISystem


class EventBus(IEventBus):
    def __init__(self) -> None:
        self._subscribers: Dict[str, List[IEventHandler | Callable[[str, ExecutionContext], None]]] = {}

    @property
    def subscribers(self) -> Dict[str, List[IEventHandler | Callable[[str, ExecutionContext], None]]]:
        return self._subscribers

    # def set_system(self, system: ISystem) -> None:
    #     self._system = system
    #     for handlers in self._subscribers.values():
    #         for handler in handlers:
    #             if isinstance(handler, EventHandler):
    #                 handler.set_system(system)

    def subscribe(self, event_name: str, handler: IEventHandler | Callable[[str, ExecutionContext], None]) -> None:
        if event_name not in self._subscribers:
            self._subscribers[event_name] = []
        self._subscribers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: IEventHandler | Callable[[str, ExecutionContext], None]) -> None:
        if event_name in self._subscribers:
            self._subscribers[event_name] = [
                h for h in self._subscribers[event_name] if h != handler
            ]
            if not self._subscribers[event_name]:
                del self._subscribers[event_name]

    def emit(self, event_name: str, context: ExecutionContext) -> None:
        for handler in self._subscribers.get(event_name, []):
            if isinstance(handler, Callable):
                handler(event_name, context)
            else:
                handler.handle(event_name, context)


class SystemBoundEventBus(IEventBus):
    def __init__(self, event_bus: IEventBus) -> None:
        super().__init__()
        self._event_bus = event_bus

    def bind_system(self, system: "ISystem") -> None:
        self._system = system
        for e in self._event_bus.subscribers.values():
            if isinstance(e, SystemBoundEventHandler):
                e.set_system(system) 

    def subscribe(self, event_name: str, handler: IEventHandler | Callable[[str, ExecutionContext], None]) -> None:
        if isinstance(handler, SystemBoundEventHandler) and self._system is not None:
            handler.set_system(self._system)
        self._event_bus.subscribe(event_name, handler)
    
    def unsubscribe(self, event_name: str, handler: IEventHandler | Callable[[str, ExecutionContext], None]) -> None:
        self._event_bus.unsubscribe(event_name, handler)

    def emit(self, event_name: str, context: ExecutionContext) -> None:
        self._event_bus.emit(event_name, context)