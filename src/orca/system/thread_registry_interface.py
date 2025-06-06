from orca.workflow_models.interfaces import ILabwareThread

from abc import ABC, abstractmethod
from typing import Generic, List, TypeVar

LabwareThreadType = TypeVar('LabwareThreadType', bound=ILabwareThread)
class IThreadRegistry(ABC, Generic[LabwareThreadType]):
    @property
    @abstractmethod
    def threads(self) -> List[LabwareThreadType]:
        raise NotImplementedError

    @abstractmethod
    def get_thread(self, id: str) -> LabwareThreadType:
        raise NotImplementedError

    @abstractmethod
    def get_thread_by_labware(self, labware_id: str) -> LabwareThreadType:
        raise NotImplementedError

    @abstractmethod
    def add_thread(self, labware_thread: LabwareThreadType) -> None:
        raise NotImplementedError


# class IThreadRegistry(ABC):
#     @property
#     @abstractmethod
#     def threads(self) -> List[ILabwareThread]:
#         raise NotImplementedError

#     @abstractmethod
#     def get_thread(self, id: str) -> ILabwareThread:
#         raise NotImplementedError

#     @abstractmethod
#     def get_thread_by_labware(self, labware_id: str) -> ILabwareThread:
#         raise NotImplementedError

#     @abstractmethod
#     def add_thread(self, labware_thread: ILabwareThread) -> None:
#         raise NotImplementedError