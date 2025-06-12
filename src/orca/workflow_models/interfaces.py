from typing import List
import typing
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location


if typing.TYPE_CHECKING:
    from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
    from orca.workflow_models.method import MethodInstance
    from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction


from abc import ABC, abstractmethod


class ILabwareThread(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def start_location(self) -> Location:
        raise NotImplementedError

    @property
    @abstractmethod
    def end_location(self) -> Location:
        raise NotImplementedError

    @property
    @abstractmethod
    def labware(self) -> LabwareInstance:
        raise NotImplementedError

    @abstractmethod
    def append_method_sequence(self, method: "MethodInstance") -> None:
        raise NotImplementedError


class IMethod(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def actions(self) -> List["UnresolvedLocationAction"]:
        raise NotImplementedError

    @abstractmethod
    def append_action(self, action: "UnresolvedLocationAction") -> None:
        raise NotImplementedError

    @abstractmethod
    def assign_thread(self, input_template: LabwareTemplate, thread: ILabwareThread) -> None:
        raise NotImplementedError