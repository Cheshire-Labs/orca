from drivers.driver_interfaces import IDriver


from abc import ABC, abstractmethod
from typing import List


class ITransporterDriver(IDriver, ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def pick(self, position_name: str, labware_type: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def place(self, position_name: str, labware_type: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError