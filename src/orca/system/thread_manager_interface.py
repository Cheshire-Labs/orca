from abc import ABC, abstractmethod


class IThreadManager(ABC):


    @abstractmethod
    async def start_all_threads(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_all_threads(self) -> None:
        raise NotImplementedError