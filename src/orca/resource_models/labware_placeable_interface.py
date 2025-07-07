from typing import Optional
from orca.resource_models.labware import LabwareInstance


from abc import ABC, abstractmethod


class ILabwarePlaceable(ABC):
    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def labware(self) -> Optional[LabwareInstance]:
        raise NotImplementedError

    def initialize_labware(self, labware: LabwareInstance) -> None:
        # TODO: Make async in future
        # TODO: this will need to be restricted to only initilaizing the labware, probably with a LabwareManager service
        raise NotImplementedError

    @abstractmethod
    async def prepare_for_pick(self, labware: LabwareInstance) -> None:
        raise NotImplementedError

    @abstractmethod
    async def prepare_for_place(self, labware: LabwareInstance) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware: LabwareInstance) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_placed(self, labware: LabwareInstance) -> None:
        raise NotImplementedError