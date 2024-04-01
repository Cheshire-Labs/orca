from abc import ABC
from typing import List

from workflow_models.status_enums import ActionStatus

class IActionObserver:
    def action_notify(self, event: ActionStatus, action: 'BaseAction') -> None:
        pass

class IAction(ABC):
    @property
    def status(self) -> ActionStatus:
        raise NotImplementedError

    def execute(self) -> None:
        raise NotImplementedError

class BaseAction(IAction, ABC):
    def __init__(self) -> None:
        self.__status: ActionStatus = ActionStatus.CREATED
        self._observers: List[IActionObserver] = []

    @property
    def status(self) -> ActionStatus:
        return self._status
    
    @property
    def _status(self) -> ActionStatus:
        return self.__status

    @_status.setter
    def _status(self, status: ActionStatus) -> None:
        self.__status = status
        for observer in self._observers:
            observer.action_notify(self.__status, self)

    def execute(self) -> None:
        if self._status == ActionStatus.COMPLETED:
            raise ValueError("Action has already been completed")
        self._statues = ActionStatus.IN_PROGRESS
        try:
            self._perform_action()
        except Exception as e:
            self._status = ActionStatus.ERRORED
            raise e        
        self._status = ActionStatus.COMPLETED

    def _perform_action(self) -> None:
        raise NotImplementedError
    
    def reset(self) -> None:
        self._status = ActionStatus.CREATED

    def add_observer(self, observer: IActionObserver) -> None:
        self._observers.append(observer)


class NullAction(BaseAction):
    def __init__(self) -> None:
        super().__init__()

    def _perform_action(self) -> None:
        pass  

