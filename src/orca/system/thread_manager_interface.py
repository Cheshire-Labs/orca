from orca.system.thread_registry_interface import IThreadRegistry


from abc import ABC, abstractmethod


class IThreadManager(IThreadRegistry, ABC):

    @abstractmethod
    async def start_all_threads(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_all_threads(self) -> None:
        raise NotImplementedError