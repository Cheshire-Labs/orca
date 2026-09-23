from abc import ABC, abstractmethod

from orca.events.execution_context import ExecutionContext


class IEventHandler(ABC):
    @abstractmethod
    def handle(self, event: str, context: ExecutionContext) -> None:
        raise NotImplementedError("Event handler must implement handle method")
