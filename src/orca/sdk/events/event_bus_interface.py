from orca.sdk.events.event_handler_interface import IEventHandler


from abc import ABC, abstractmethod
from typing import Callable, Dict, List

from orca.sdk.events.execution_context import ExecutionContext



class IEventBus(ABC):
    @property
    def subscribers(self) -> Dict[str, List[IEventHandler | Callable[[str, ExecutionContext], None]]]:
        raise NotImplementedError
    
    @abstractmethod
    def subscribe(self, event_name: str, handler: IEventHandler | Callable[[str, ExecutionContext], None]) -> None:
        raise NotImplementedError

    @abstractmethod
    def unsubscribe(self, event_name: str, handler: IEventHandler | Callable[[str, ExecutionContext], None]) -> None:
        raise NotImplementedError

    @abstractmethod
    def emit(self, event_name: str, context: ExecutionContext) -> None:
        raise NotImplementedError
    