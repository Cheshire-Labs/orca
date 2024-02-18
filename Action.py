from abc import ABC
from drivers.base_resource import IResource
from labware import Labware
from method import MethodStatus


from typing import Any, Dict, List, Optional

from resource_pool import ResourcePool


class IAction(ABC):
    @property
    def status(self) -> MethodStatus:
        raise NotImplementedError

    def set_status(self, status: MethodStatus) -> None:
        raise NotImplementedError

    def execute(self) -> None:
        raise NotImplementedError

class Action(IAction, ABC):
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
    