from abc import ABC, abstractmethod
from typing import List, Protocol

from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction


class IHasLabware(Protocol):
    """Anything carrying a LabwareInstance. ILabwareThread satisfies this
    structurally; declared here so IMethod can name what it actually needs
    without depending on the full thread interface."""
    @property
    def labware(self) -> LabwareInstance: ...


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
    def actions(self) -> List[UnresolvedLocationAction]:
        raise NotImplementedError

    @abstractmethod
    def append_action(self, action: UnresolvedLocationAction) -> None:
        raise NotImplementedError

    @abstractmethod
    def assign_thread(
        self,
        input_template: LabwareTemplate,
        thread: IHasLabware,
    ) -> None:
        raise NotImplementedError


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
    def end_locations(self) -> list[Location]:
        raise NotImplementedError

    @property
    @abstractmethod
    def labware(self) -> LabwareInstance:
        raise NotImplementedError

    @abstractmethod
    def append_method_sequence(self, method: IMethod) -> None:
        raise NotImplementedError
