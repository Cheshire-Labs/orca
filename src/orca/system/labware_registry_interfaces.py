from orca.resource_models.labware import Labware, LabwareTemplate

from abc import ABC, abstractmethod
from typing import List


class ILabwareRegistry(ABC):

    @property
    @abstractmethod
    def labwares(self) -> List[Labware]:
        pass

    @abstractmethod
    def get_labware(self, name: str) -> Labware:
        raise NotImplementedError

    @abstractmethod
    def add_labware(self, labware: Labware) -> None:
        raise NotImplementedError


class ILabwareTemplateRegistry(ABC):
    @abstractmethod
    def get_labware_template(self, name: str) -> LabwareTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_labware_template(self, labware: LabwareTemplate) -> None:
        raise NotImplementedError