from typing import Optional

from typing import Any, Dict
from resource_models.base_resource import Equipment, LabwareLoadable
from resource_models.labware import Labware


from abc import ABC

from resource_models.plate_pad import PlatePad
from typing import List


class Location(LabwareLoadable, ABC):
    def __init__(self, teachpoint_name: str, resource: Optional[Equipment] = None) -> None:
        self._teachpoint_name = teachpoint_name
        self._resource: Equipment = resource if resource else PlatePad(teachpoint_name)
        self._options: Dict[str, Any] = {}

    @property
    def teachpoint_name(self) -> str:
        return self._teachpoint_name

    @property
    def labware(self) -> Optional[Labware]:
        return self._resource.labware

    @property
    def is_busy(self) -> bool:
        return self._resource.labware is not None

    @property
    def resource(self) -> Optional[Equipment]:
        return self._resource
    
    @resource.setter
    def resource(self, resource: Equipment) -> None:
        self._resource = resource

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
    