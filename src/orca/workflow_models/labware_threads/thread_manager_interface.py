from abc import ABC, abstractmethod
from typing import List

from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread


class IThreadManager(ABC):

    @abstractmethod
    def has_completed(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def active_threads(self) -> List[ExecutingLabwareThread]:
        raise NotImplementedError

    @property
    @abstractmethod
    def executing_threads(self) -> List[ExecutingLabwareThread]:
        raise NotImplementedError

    @abstractmethod
    async def start_all_threads(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_all_threads(self) -> None:
        raise NotImplementedError