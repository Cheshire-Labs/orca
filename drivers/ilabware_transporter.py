from drivers.base_resource import IResource


from abc import abstractmethod
from typing import List


class ILabwareTransporter(IResource):

    @abstractmethod
    def pick(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def place(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_plate_type(self, plate_type: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError