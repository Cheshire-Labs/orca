from orca.system.interfaces import ISystem


from abc import ABC, abstractmethod
from typing import Generic, Optional, TypeVar

T = TypeVar("T")

class EventHandler(ABC, Generic[T]):
    def __init__(self) -> None:
        self.system: Optional[ISystem] = None

    def set_system(self, system: ISystem) -> None:
        self.system = system

    @abstractmethod
    def handle(self, event: str, data: T) -> None:
        raise NotImplementedError("Event handler must implement handle method")