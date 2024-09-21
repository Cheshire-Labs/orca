from typing import Optional

from typing import Any, Dict
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.base_resource import ILabwarePlaceable
from orca.resource_models.labware import Labware


from abc import ABC


from typing import List



class IResourceLocationObserver(ABC):
    def location_notify(self, event: str, location: "Location", resource: ILabwarePlaceable) -> None:
        pass

class ILabwareLocationObserver(ABC):
    def notify_labware_location_change(self, event: str, location: "Location", labware: Labware) -> None:
        pass

class Location(ILabwarePlaceable):
    def __init__(self, teachpoint_name: str, resource: Optional[ILabwarePlaceable] = None) -> None:
        self._teachpoint_name = teachpoint_name
        self._resource: ILabwarePlaceable = resource if resource else PlatePad(teachpoint_name)
        self._options: Dict[str, Any] = {}
        self._resource_observers: List[IResourceLocationObserver] = []
        self._labware_observers: List[ILabwareLocationObserver] = []
    
    @property
    def name(self) -> str:
        return self._teachpoint_name
                              
    @property
    def teachpoint_name(self) -> str:
        # TODO: this is redundant to name, these should be refactored into one or differentiated
        return self._teachpoint_name

    @property
    def labware(self) -> Optional[Labware]:
        return self._resource.labware
    
    def initialize_labware(self, labware: Labware) -> None:
        # TODO: this will need to be restricted to only initilaizing the labware
        self._resource.initialize_labware(labware)

    @property
    def resource(self) -> ILabwarePlaceable:
        return self._resource
    
    @resource.setter
    def resource(self, resource: ILabwarePlaceable) -> None:
        self._resource = resource
        for obeserver in self._resource_observers:
            obeserver.location_notify("resource_set", self, resource)
    
    def set_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    async def prepare_for_place(self, labware: Labware) -> None:
        await self._resource.prepare_for_place(labware)

    async def prepare_for_pick(self, labware: Labware) -> None:
        await self._resource.prepare_for_pick(labware)

    async def notify_picked(self, labware: Labware) -> None:
        await self._resource.notify_picked(labware)
        for observer in self._labware_observers:
            observer.notify_labware_location_change("picked", self, labware)
    
    async def notify_placed(self, labware: Labware) -> None:
        await self._resource.notify_placed(labware)
        for observer in self._labware_observers:
            observer.notify_labware_location_change("placed", self, labware)

    def __str__(self) -> str:
        return f"Location: {self._teachpoint_name}"
    
    def add_observer(self, observer: IResourceLocationObserver | ILabwareLocationObserver) -> None:
        if isinstance(observer, ILabwareLocationObserver):
            if observer in self._labware_observers:
                return
            self._labware_observers.append(observer)
        elif isinstance(observer, IResourceLocationObserver):
            if observer in self._resource_observers:
                return
            self._resource_observers.append(observer)
        else:
            raise NotImplementedError(f"Observer type {type(observer)} not supported")
    