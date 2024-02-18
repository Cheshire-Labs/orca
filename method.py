
from enum import Enum, auto
from typing import List, Optional
from Action import IAction

from method_status import MethodStatus

class Method:

    def __init__(self, name: str):
        self._name = name
        self._actions: List[IAction] = []
        self._status = MethodStatus.CREATED

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def status(self) -> MethodStatus:
        if all(action.status == MethodStatus.CANCELED for action in self._actions):
            return MethodStatus.CANCELED
        elif all(action.status in [MethodStatus.COMPLETED, MethodStatus.CANCELED] for action in self._actions):
            return MethodStatus.COMPLETED
        elif any(action.status == MethodStatus.ERRORED for action in self._actions):
            return MethodStatus.ERRORED
        elif any(action.status == MethodStatus.PAUSED for action in self._actions):
            return MethodStatus.PAUSED
        elif any(action.status == MethodStatus.AWAITING_RESOURCES for action in self._actions):
            return MethodStatus.AWAITING_RESOURCES
        elif any(action.status == MethodStatus.RUNNING for action in self._actions):
            return MethodStatus.RUNNING
        elif all(action.status == MethodStatus.READY for action in self._actions):
            return MethodStatus.READY
        elif all(action.status == MethodStatus.QUEUED for action in self._actions):
            return MethodStatus.QUEUED
        else:
            return MethodStatus.CREATED

    @property
    def actions(self) -> List[IAction]:
        return self._actions

    def append_action(self, action: IAction):
        self._actions.append(action)
    
    def get_next_action(self) -> Optional[IAction]:
        return next((action 
                     for action in self._actions 
                     if action.status in [MethodStatus.AWAITING_RESOURCES, MethodStatus.READY, MethodStatus.QUEUED]), None)
