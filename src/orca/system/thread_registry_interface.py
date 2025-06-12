from abc import ABC, abstractmethod
from typing import List

from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance

# LabwareThreadType = TypeVar('LabwareThreadType', bound=ILabwareThread)
# class IThreadRegistry(ABC, Generic[LabwareThreadType]):
#     @property
#     @abstractmethod
#     def threads(self) -> List[LabwareThreadType]:
#         raise NotImplementedError

#     @abstractmethod
#     def get_thread(self, id: str) -> LabwareThreadType:
#         raise NotImplementedError

#     @abstractmethod
#     def get_thread_by_labware(self, labware_id: str) -> LabwareThreadType:
#         raise NotImplementedError

#     @abstractmethod
#     def add_thread(self, labware_thread: LabwareThreadType) -> None:
#         raise NotImplementedError


class IThreadRegistry(ABC):
    @property
    @abstractmethod
    def threads(self) -> List[LabwareThreadInstance]:
        raise NotImplementedError

    @abstractmethod
    def get_thread(self, id: str) -> LabwareThreadInstance:
        raise NotImplementedError

    @abstractmethod
    def get_thread_by_labware(self, labware_id: str) -> LabwareThreadInstance:
        raise NotImplementedError

    @abstractmethod
    def add_thread(self, labware_thread: LabwareThreadInstance) -> None:
        raise NotImplementedError