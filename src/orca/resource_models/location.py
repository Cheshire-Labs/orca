from abc import ABC
import asyncio
from typing import List, Optional, Any, Dict
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from orca.resource_models.labware import LabwareInstance

class IResourceLocationObserver(ABC):
    def location_notify(self, event: str, location: "Location", resource: ILabwarePlaceable) -> None:
        pass

class ILabwareLocationObserver(ABC):
    def notify_labware_location_change(self, event: str, location: "Location", labware: LabwareInstance) -> None:
        pass

class Location(ILabwarePlaceable):
    def __init__(self, teachpoint_name: str, resource: Optional[ILabwarePlaceable] = None) -> None:
        self._teachpoint_name = teachpoint_name
        self._resource: ILabwarePlaceable = resource if resource else PlatePad(teachpoint_name)
        self._options: Dict[str, Any] = {}
        self._resource_observers: List[IResourceLocationObserver] = []
        self._labware_observers: List[ILabwareLocationObserver] = []
        self._availability_condition = asyncio.Condition()  # Event-driven availability notification
    
    @property
    def name(self) -> str:
        return self._teachpoint_name
                              
    @property
    def teachpoint_name(self) -> str:
        # TODO: this is redundant to name, these should be refactored into one or differentiated
        return self._teachpoint_name

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._resource.labware
    
    def initialize_labware(self, labware: LabwareInstance) -> None:
        # TODO: this will need to be restricted to only initilaizing the labware
        self._resource.initialize_labware(labware)

    @property
    def resource(self) -> ILabwarePlaceable:
        return self._resource

    @property
    def supports_deadlock_resolution(self) -> bool:
        """Delegate to the underlying resource."""
        return self._resource.supports_deadlock_resolution

    @resource.setter
    def resource(self, resource: ILabwarePlaceable) -> None:
        self._resource = resource
        for obeserver in self._resource_observers:
            obeserver.location_notify("resource_set", self, resource)
    
    def set_options(self, options: Dict[str, Any]) -> None:
        self._options = options

    async def prepare_for_place(self, labware: LabwareInstance) -> None:
        await self._resource.prepare_for_place(labware)

    async def prepare_for_pick(self, labware: LabwareInstance) -> None:
        await self._resource.prepare_for_pick(labware)

    async def notify_picked(self, labware: LabwareInstance) -> None:
        await self._resource.notify_picked(labware)
        for observer in self._labware_observers:
            observer.notify_labware_location_change("picked", self, labware)

        # Notify all threads waiting for this location to become available
        async with self._availability_condition:
            self._availability_condition.notify_all()
    
    async def notify_placed(self, labware: LabwareInstance) -> None:
        await self._resource.notify_placed(labware)
        for observer in self._labware_observers:
            observer.notify_labware_location_change("placed", self, labware)

    async def wait_until_available(self, timeout: Optional[float] = None) -> None:
        """
        Wait until this location becomes available (empty).
        Event-driven - instant notification when labware is picked.

        Args:
            timeout: Optional timeout in seconds. If None, waits indefinitely.

        Raises:
            asyncio.TimeoutError: If timeout expires before location becomes available.
        """
        async with self._availability_condition:
            while self.labware is not None:
                if timeout:
                    await asyncio.wait_for(self._availability_condition.wait(), timeout)
                else:
                    await self._availability_condition.wait()

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
    