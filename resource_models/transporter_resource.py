import asyncio
from resource_models.location import Location
from resource_models.base_resource import BaseResource
from abc import abstractmethod
from typing import List, Optional
from resource_models.labware import Labware


class TransporterResource(BaseResource):
    def __init__(self, name: str):
        super().__init__(name)
        self.__labware: Optional[Labware] = None
        self.wait_on_available = asyncio.Event()

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware
    
    @property
    def _labware(self) -> Optional[Labware]:
        return self.__labware
    
    @_labware.setter
    def _labware(self, labware: Optional[Labware]) -> None:
        if labware is None:
            self.wait_on_available.set()
        else:
            self.wait_on_available.clear()
        self.__labware = labware
    

     
    @abstractmethod
    async def pick(self, location: Location) -> None:
        raise NotImplementedError

    @abstractmethod
    async def place(self, location: Location) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        raise NotImplementedError
    
    def __str__(self) -> str:
        return f"Transporter: {self._name}"
    