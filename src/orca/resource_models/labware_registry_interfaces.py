from orca.resource_models.labware import LabwareInstance, LabwareTemplate

from abc import ABC, abstractmethod
from typing import List


class ILabwareRegistry(ABC):

    @property
    @abstractmethod
    def labwares(self) -> List[LabwareInstance]:
        pass

    @abstractmethod
    def get_labware(self, name: str) -> LabwareInstance:
        raise NotImplementedError

    @abstractmethod
    def add_labware(self, labware: LabwareInstance) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_labware(self, labware_id: str) -> LabwareInstance | None:
        """Remove a labware by id. Returns the removed instance or None.

        Used by the operator clear surfaces.
        """
        raise NotImplementedError


class ILabwareTemplateRegistry(ABC):
    @abstractmethod
    def get_labware_template(self, name: str) -> LabwareTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_labware_template(self, labware: LabwareTemplate) -> None:
        raise NotImplementedError

    @property
    @abstractmethod
    def labware_templates(self) -> List[LabwareTemplate]:
        """Every registered labware template. Order is insertion order."""
        raise NotImplementedError