from abc import ABC
from enum import Enum, auto
from resource_models.location import Location
from resource_models.transporter_resource import TransporterResource
from resource_models.base_resource import LabwareLoadable
from resource_models.labware import Labware
    

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

class UnloadLabwareAction(BaseAction):
    def __init__(self, resource: LabwareLoadable, labware: Labware) -> None:
        super().__init__()
        self._resource = resource
        self._labware = labware

    def _perform_action(self) -> None:
        self._resource.unload_labware(self._labware)



class LoadLabwareAction(BaseAction):
    def __init__(self, resource: LabwareLoadable, labware: Labware) -> None:
        super().__init__()
        self._resource = resource
        self._labware = labware

    def _perform_action(self) -> None:
        self._resource.load_labware(self._labware)


class PickAction(BaseAction):
    def __init__(self, transporter: TransporterResource, labware: Labware, src_location: Location) -> None:
        super().__init__()
        self._transporter = transporter
        self._labware = labware
        self._src_location = src_location

    def _perform_action(self) -> None:
        self._transporter.pick(self._src_location)


class PlaceAction(BaseAction):
    def __init__(self, transporter: TransporterResource, labware: Labware, target_location: Location) -> None:
        super().__init__()
        self._transporter = transporter
        self._labware = labware
        self._target_location = target_location

    def _perform_action(self) -> None:
        self._transporter.place(self._target_location)
