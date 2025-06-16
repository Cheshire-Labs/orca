from orca.events.event_handler_interface import IEventHandler


from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Union

from orca.events.execution_context import ExecutionContext

EventHandlerType = Union[
    IEventHandler,
    Callable[[str, ExecutionContext], None]
]

class IEventBus(ABC):
    @property
    def subscribers(self) -> Dict[str, List[EventHandlerType]]:
        raise NotImplementedError
    
    @abstractmethod
    def subscribe(self, event_name: str, handler: EventHandlerType) -> None:
        raise NotImplementedError

    @abstractmethod
    def unsubscribe(self, event_name: str, handler: EventHandlerType) -> None:
        raise NotImplementedError

    @abstractmethod
    def emit(self, event_name: str, context: ExecutionContext) -> None:
        raise NotImplementedError
    