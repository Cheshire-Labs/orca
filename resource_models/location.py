from base_resource import EquipmentResource, ILabwareLoadable, IResource, IUseable, ResourceUnavailableError
from typing import Any, Dict, List, Optional

from resource_models.labware import Labware


class Location(IUseable, IResource, ILabwareLoadable):
    @property
    def location(self) -> str:
        return self._name
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def in_use(self) -> bool:
        return self._labware is not None

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware
    
    @property
    def resource(self) -> Optional[EquipmentResource]:
        return self._resource

    def __init__(self, name: str) -> None:
        self._name = name
        self._reserved = False
        self._resource: Optional[EquipmentResource] = None
        self._can_resolve_deadlock = True
        self._options:  Dict[str, Any] = {}
        self._init_options: Dict[str, Any] = {}
        self._labware: Optional[Labware] = None

    def set_resource(self, resource: EquipmentResource, can_resolve_deadlock: bool = False) -> None:
        if self._resource is not None:
            raise ResourceUnavailableError(f"Error assigning resource '{resource.name}' to location '{self._name}'. Location already has a resource set to '{self._resource.name}'")
        self._can_resolve_deadlock = can_resolve_deadlock
        self._resource = resource

    def load_labware(self, labware: Labware) -> None:
        if self._labware is not None:
            raise ResourceUnavailableError(f"Error setting labware '{labware}' to location '{self}'.  Location is already occupied by '{self._labware}'")
        self._labware = labware

    def unload_labware(self, labware: Labware) -> None:
        if self._labware != labware:
            raise ResourceUnavailableError(f"Error unloading labware '{labware}' from location '{self}'.  Location does not contain '{labware}' location is occupied by '{self._labware}'")
        self._labware = None

    def set_init_options(self, options: Dict[str, Any]) -> None:
        self._init_options = options

    def set_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    def __str__(self) -> str:
        return self._name