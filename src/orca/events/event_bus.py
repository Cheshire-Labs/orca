import logging
from typing import Dict, List

from orca.events.event_bus_interface import EventHandlerType, IEventBus
from orca.events.event_handlers import SystemBoundEventHandler
from orca.events.execution_context import ExecutionContext
from orca.system.system_interface import ISystem

orca_logger = logging.getLogger("orca")


class EventBus(IEventBus):
    """EventBus implementation: subscribe to events, unsubscribe, and emit.

    Supports both callable handlers and SystemBoundEventHandler instances.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[EventHandlerType]] = {}
        self._global_subscribers: List[EventHandlerType] = []

    @property
    def subscribers(self) -> Dict[str, List[EventHandlerType]]:
        return self._subscribers

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

    def subscribe_all(self, handler: EventHandlerType) -> None:
        self._global_subscribers.append(handler)

    def _invoke_handler(self, handler: EventHandlerType, event_name: str, context: ExecutionContext) -> None:
        try:
            if callable(handler):
                handler(event_name, context)
            else:
                handler.handle(event_name, context)
        except Exception:
            orca_logger.error(
                "Handler %s failed on event %s", handler, event_name, exc_info=True
            )

    def emit(self, event_name: str, context: ExecutionContext) -> None:
        # Call handlers for the exact event_name
        handled_event_names = set([event_name])
        for handler in self._subscribers.get(event_name, []):
            self._invoke_handler(handler, event_name, context)

        # Handle generalized METHOD.STATUS_NAME events
        parts = event_name.split(".")
        if len(parts) == 3:
            generalized_event_name = f"{parts[0]}.{parts[2]}"
            if generalized_event_name not in handled_event_names:
                for handler in self._subscribers.get(generalized_event_name, []):
                    self._invoke_handler(handler, generalized_event_name, context)

        # Notify global subscribers (system-level event forwarding)
        for handler in self._global_subscribers:
            self._invoke_handler(handler, event_name, context)



class SystemBoundEventBus(IEventBus):
    def __init__(self, event_bus: IEventBus) -> None:
        super().__init__()
        self._event_bus = event_bus

    def bind_system(self, system: ISystem) -> None:
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

    def subscribe_all(self, handler: EventHandlerType) -> None:
        self._event_bus.subscribe_all(handler)

    def emit(self, event_name: str, context: ExecutionContext) -> None:
        self._event_bus.emit(event_name, context)
