from orca.events.event_bus_interface import EventHandlerType, IEventBus

from typing import TYPE_CHECKING, Dict, List

from orca.events.event_handlers import SystemBoundEventHandler
from orca.events.execution_context import ExecutionContext

if TYPE_CHECKING:
    from orca.system.system_interface import ISystem



class EventBus(IEventBus):
    """ A simple event bus implementation that allows subscribing to events, unsubscribing, and emitting events.
    It supports both callable handlers and instances of `SystemBoundEventHandler`."""
    def __init__(self) -> None:
        """ Initializes the EventBus instance.
        """
        self._subscribers: Dict[str, List[EventHandlerType]] = {}

    @property
    def subscribers(self) -> Dict[str, List[EventHandlerType]]:
        return self._subscribers

    # def set_system(self, system: ISystem) -> None:
    #     self._system = system
    #     for handlers in self._subscribers.values():
    #         for handler in handlers:
    #             if isinstance(handler, EventHandler):
    #                 handler.set_system(system)

    def subscribe(self, event_name: str, handler: EventHandlerType) -> None:
        if event_name not in self._subscribers:
            self._subscribers[event_name] = []
        self._subscribers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandlerType) -> None:
        if event_name in self._subscribers:
            self._subscribers[event_name] = [
                h for h in self._subscribers[event_name] if h != handler
            ]
            if not self._subscribers[event_name]:
                del self._subscribers[event_name]

    def emit(self, event_name: str, context: ExecutionContext) -> None:
        # Call handlers for the exact event_name
        handled_event_names = set([event_name])
        for handler in self._subscribers.get(event_name, []):
            if callable(handler):
                handler(event_name, context)
            else:
                handler.handle(event_name, context)

        # Handle generalized METHOD.STATUS_NAME events
        parts = event_name.split(".")
        if len(parts) == 3:
            generalized_event_name = f"{parts[0]}.{parts[2]}"
            if generalized_event_name not in handled_event_names:
                for handler in self._subscribers.get(generalized_event_name, []):
                    if callable(handler):
                        handler(generalized_event_name, context)
                    else:
                        handler.handle(generalized_event_name, context)



class SystemBoundEventBus(IEventBus):
    def __init__(self, event_bus: IEventBus) -> None:
        super().__init__()
        self._event_bus = event_bus

    def bind_system(self, system: "ISystem") -> None:
        self._system = system
        for handler_list in self._event_bus.subscribers.values():
            for handler in handler_list:
                if isinstance(handler, SystemBoundEventHandler):
                    handler.set_system(system) 

    def subscribe(self, event_name: str, handler: EventHandlerType) -> None:
        if isinstance(handler, SystemBoundEventHandler) and self._system is not None:
            handler.set_system(self._system)
        self._event_bus.subscribe(event_name, handler)
    
    def unsubscribe(self, event_name: str, handler: EventHandlerType) -> None:
        self._event_bus.unsubscribe(event_name, handler)

    def emit(self, event_name: str, context: ExecutionContext) -> None:
        self._event_bus.emit(event_name, context)