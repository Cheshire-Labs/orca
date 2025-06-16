from orca.events.execution_context import ExecutionContext

from abc import ABC, abstractmethod
from typing import TypeVar

T = TypeVar("T")

class IEventHandler(ABC):
    @abstractmethod
    def handle(self, event: str, data: ExecutionContext) -> None:
        raise NotImplementedError("Event handler must implement handle method")


