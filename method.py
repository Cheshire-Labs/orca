
from enum import Enum, auto
from typing import Any, Dict, List, Optional
from drivers.base_resource import IResource
from labware import Labware

class MethodStatus(Enum):
    CREATED = auto()
    QUEUED = auto()
    AWAITING_RESOURCES = auto()
    READY = auto()
    RUNNING = auto()
    PAUSED = auto()
    ERRORED = auto()
    COMPLETED = auto()
    CANCELED = auto()

class Action:
    def __init__(self, resource: IResource, command: str, options: Optional[Dict[str, Any]] =None, inputs: Optional[List[Labware]] = None, outputs: Optional[List[Labware]] = None):
        self._resource = resource
        self._command = command
        self._options: Dict[str, Any] = {} if options is None else options
        self._inputs: List[Labware] = inputs if inputs is not None else []
        self._output: List[Labware] = outputs if outputs is not None else []
        self._status: MethodStatus = MethodStatus.CREATED
    
    @property
    def status(self) -> MethodStatus:
        return self._status
    
    def set_status(self, status: MethodStatus) -> None:
        self._status = status

    def execute(self) -> None:
        self._resource.set_command(self._command) 
        self._resource.set_command_options(self._options)
        self._status = MethodStatus.RUNNING
        self._resource.execute()
        self._status = MethodStatus.COMPLETED


class Method:

    def __init__(self, name: str):
        self._name = name
        self._actions: List[Action] = []
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
    def actions(self) -> List[Action]:
        return self._actions

    def append_action(self, action: Action):
        self._actions.append(action)
    
    def get_next_action(self) -> Optional[Action]:
        return next((action 
                     for action in self._actions 
                     if action.status in [MethodStatus.AWAITING_RESOURCES, MethodStatus.READY, MethodStatus.QUEUED]), None)
