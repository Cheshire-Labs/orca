from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.interfaces import IReservationManager


import asyncio
import uuid
from typing import Callable, Optional


class LocationReservation:
    def __init__(self, requested_location: Location, labware: LabwareInstance | None = None) -> None:
        self._id = str(uuid.uuid4())
        self._labware = labware
        self._requested_location: Location = requested_location
        self._reserved_location: Optional[Location] = None
        self._reservation_manager: IReservationManager | None = None
        self._reservation_release_callback: Callable[[], None] = lambda: None
        self.processed: asyncio.Event = asyncio.Event()
        self.granted: asyncio.Event  = asyncio.Event()
        self.deadlocked: asyncio.Event = asyncio.Event()
        self.rejected: asyncio.Event = asyncio.Event()

    @property
    def id(self) -> str:
        return self._id

    @property
    def labware(self) -> LabwareInstance | None:
        return self._labware

    def set_location(self, location: Location) -> None:
        if self.rejected.is_set():
            raise ValueError("Reservation has been rejected")
        self._reserved_location = location

    @property
    def requested_location(self) -> Location:
        return self._requested_location

    @property
    def reserved_location(self) -> Location:
        if self._reserved_location is None:
            raise ValueError("Location not yet reserved")
        return self._reserved_location

    # def submit_reservation_request(self, reservation_manager: IThreadReservationCoordinator) -> None:
    #     self._reservation_manager = reservation_manager
    #     reservation_manager.submit_reservation_request(self._requested_location.name, self) 

    def set_reservation_release_callback(self, callback: Callable[[], None]) -> None:
        """Sets a callback to be called when the reservation is released."""
        self._reservation_release_callback = callback

    def release_reservation(self) -> None:
        self._reservation_release_callback()

    def clear(self) -> None:
        """Clears the reservation, resetting the reserved location."""
        if self.granted.is_set():
            # May re-examine if this should be allowed - I haven't looked into the implications yet
            raise ValueError("Cannot clear a granted reservation")
        self.processed.clear()
        self.deadlocked.clear()
        self.rejected.clear()