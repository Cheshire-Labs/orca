from resource_models.base_resource import BaseResource


from abc import ABC, abstractmethod
from typing import List

from resource_models.loadable_resources.location import Location


class TransporterResource(BaseResource, ABC):
    def __init__(self, name: str):
        super().__init__(name)

    @abstractmethod
    def pick(self, location: Location) -> None:
        raise NotImplementedError

    @abstractmethod
    def place(self, location: Location) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError