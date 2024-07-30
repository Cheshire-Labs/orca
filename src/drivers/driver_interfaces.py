from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class IInitializableDriver(ABC):
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        raise NotImplementedError

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError


class IDriver(IInitializableDriver, ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        raise NotImplementedError


class ILabwarePlaceableDriver(IDriver, ABC):

    @abstractmethod
    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        raise NotImplementedError