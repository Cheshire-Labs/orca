from resource_models.labware import Labware, LabwareTemplate


from abc import ABC, abstractmethod
from typing import List

from workflow_models.workflow import LabwareThread, Method, Workflow

class ILabwareRegistry(ABC):
    
    @property
    @abstractmethod
    def labwares(self) -> List[Labware]:
        pass
    
    @abstractmethod
    def get_labware(self, name: str) -> Labware:
        raise NotImplementedError

    @abstractmethod
    def add_labware(self, labware: Labware) -> None:
        raise NotImplementedError


class IWorkflowRegistry(ABC):
    @abstractmethod
    def get_workflow(self, name: str) -> Workflow:
        raise NotImplementedError
    
    @abstractmethod
    def add_workflow(self, workflow: Workflow) -> None:
        raise NotImplementedError
    
class IMethodRegistry(ABC):
    @abstractmethod
    def get_method(self, name: str) -> Method:
        raise NotImplementedError
    
    @abstractmethod
    def add_method(self, method: Method) -> None:
        raise NotImplementedError


class ILabwareTemplateRegistry(ABC):
    @abstractmethod
    def get_labware_template(self, name: str) -> LabwareTemplate:
        raise NotImplementedError

    @abstractmethod
    def add_labware_template(self, labware: LabwareTemplate) -> None:
        raise NotImplementedError


class ILabwareThreadRegisty(ABC):
    @abstractmethod
    def get_labware_thread(self, name: str) -> LabwareThread:
        raise NotImplementedError

    @abstractmethod
    def add_labware_thread(self, labware_thread: LabwareThread) -> None:
        raise NotImplementedError



    