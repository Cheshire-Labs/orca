from resource_models.location import Location
from resource_models.base_resource import BaseResource

from abc import abstractmethod
from typing import List, Optional
from resource_models.labware import Labware


class TransporterResource(BaseResource, Location):
    def __init__(self, name: str):
        super().__init__(name)
        self._labware: Optional[Labware] = None

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware

    @abstractmethod
    def pick(self, location: Location) -> None:
        raise NotImplementedError

    @abstractmethod
    def place(self, location: Location) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError