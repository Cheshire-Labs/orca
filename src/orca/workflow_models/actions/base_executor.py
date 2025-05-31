from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.status_enums import ActionStatus


import asyncio
from abc import ABC, abstractmethod


class ExecutingActionDecorator(ABC):
    def __init__(self):
        self._is_executing = asyncio.Lock()
        self.status = ActionStatus.CREATED

    @property
    @abstractmethod
    def status(self) -> ActionStatus:
        raise NotImplementedError

    @status.setter
    @abstractmethod
    def status(self, status: ActionStatus) -> None:
        raise NotImplementedError

    @abstractmethod
    async def _execute_action(self) -> None:
        raise NotImplementedError

    async def execute(self) -> None:
        async with self._is_executing:
            if self.status == ActionStatus.COMPLETED:
                return
            if self.status == ActionStatus.ERRORED:
                raise ValueError("Action has errored, cannot execute")
            try:
                await self._execute_action()
            except Exception as e:
                self.status = ActionStatus.ERRORED
                raise e
            self.status = ActionStatus.COMPLETED