from typing import Optional

from typing import Any, Dict
from resource_models.base_resource import LabwarePlaceable
from resource_models.labware import Labware


from abc import ABC

from resource_models.plate_pad import PlatePad
from typing import List


class ILocationObeserver(ABC):
    def location_notify(self, event: str, location: "Location", resource: LabwarePlaceable) -> None:
        pass

class Location(LabwarePlaceable, ABC):
    def __init__(self, teachpoint_name: str, resource: Optional[LabwarePlaceable] = None) -> None:
        self._teachpoint_name = teachpoint_name
        self._resource: LabwarePlaceable = resource if resource else PlatePad(teachpoint_name)
        self._options: Dict[str, Any] = {}
        self._observers: List[ILocationObeserver] = []
                              
    @property
    def teachpoint_name(self) -> str:
        return self._teachpoint_name

    @property
    def labware(self) -> Optional[Labware]:
        return self._resource.labware
    
    def set_labware(self, labware: Labware) -> None:
        # TODO: this will need to be restricted to only initilaizing the labware
        self._resource.set_labware(labware)

    @property
    def is_available(self) -> bool:
        return self._resource.labware is None

    @property
    def resource(self) -> LabwarePlaceable:
        return self._resource
    
    @resource.setter
    def resource(self, resource: LabwarePlaceable) -> None:
        self._resource = resource
        for obeserver in self._observers:
            obeserver.location_notify("resource_set", self, resource)

    def set_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    def prepare_for_place(self, labware: Labware) -> None:
        self._resource.prepare_for_place(labware)

    def prepare_for_pick(self, labware: Labware) -> None:
        self._resource.prepare_for_pick(labware)

    def notify_picked(self, labware: Labware) -> None:
        self._resource.notify_picked(labware)
    
    def notify_placed(self, labware: Labware) -> None:
        self._resource.notify_placed(labware)

    def __str__(self) -> str:
        return f"Location: {self._teachpoint_name}"
    
    def add_observer(self, observer: ILocationObeserver) -> None:
        self._observers.append(observer)
    