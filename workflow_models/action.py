from abc import ABC
from enum import Enum, auto

    

class ActionStatus(Enum):
    CREATED = auto()
    READY = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    # CANCELLED = auto() # TODO: Implement


class IAction(ABC):
    @property
    def status(self) -> ActionStatus:
        raise NotImplementedError

    def execute(self) -> None:
        raise NotImplementedError

class BaseAction(IAction, ABC):
    def __init__(self) -> None:
        self._status: ActionStatus = ActionStatus.CREATED

    @property
    def status(self) -> ActionStatus:
        return self._status

    def execute(self) -> None:
        self._status = ActionStatus.IN_PROGRESS
        try:
            self._perform_action()
        except Exception as e:
            self._status = ActionStatus.FAILED
            raise e        
        self._status = ActionStatus.COMPLETED

    def _perform_action(self) -> None:
        raise NotImplementedError


class NullAction(BaseAction):
    def __init__(self) -> None:
        super().__init__()

    def _perform_action(self) -> None:
        pass  


