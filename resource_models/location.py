from typing import Optional
from resource_models.base_resource import LabwareLoadable
from resource_models.labware import Labware


from abc import ABC

from resource_models.plate_pad import PlatePad


class Location(LabwareLoadable, ABC):
    def __init__(self, teachpoint_name: str, resource: Optional[LabwareLoadable] = None) -> None:
        self._teachpoint_name = teachpoint_name
        self._resource: LabwareLoadable = resource if resource else PlatePad(teachpoint_name)

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
    def resource(self) -> Optional[LabwareLoadable]:
        return self._resource
    
    @resource.setter
    def resource(self, resource: LabwareLoadable) -> None:
        self._resource = resource

    def load_labware(self, labware: Labware) -> None:
        self._resource.load_labware(labware)

    def unload_labware(self, labware: Labware) -> None:
        self._resource.unload_labware(labware)