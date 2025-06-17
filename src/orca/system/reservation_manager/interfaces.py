from abc import ABC, abstractmethod
import asyncio
from typing import List
import typing
from orca.resource_models.location import Location

if typing.TYPE_CHECKING:
    from orca.system.reservation_manager.location_reservation import LocationReservation


class ILabwareLocationManager(ABC):
    def is_occupied(self, location: Location) -> bool:
        raise NotImplementedError


class IAvailabilityManager(ABC):
    def is_location_available(self, location: Location) -> bool:
        raise NotImplementedError

    async def await_available(self, location: Location) -> None:
        raise NotImplementedError


class IReservationManager(ABC):
    def can_reserve(self, location_name: str) -> bool:
        raise NotImplementedError

    def release_reservation(self, location_name: str) -> None:
        raise NotImplementedError


class IReservationCollection(ABC):
    @property
    @abstractmethod
    def thread_id(self) -> str:
        """Returns the ID of the thread that owns this reservation collection."""
        raise NotImplementedError

    @abstractmethod
    def get_reservations(self) -> List["LocationReservation"]:
        """Returns a list of all reservations."""
        raise NotImplementedError

    @abstractmethod
    def resolve_final_reservation(self) -> None:
        """Resolves the final reservation in the collection, marking it as processed."""
        raise NotImplementedError

    @property
    @abstractmethod
    def processed(self) -> asyncio.Event:
        """An event that is set when the reservation collection has been processed."""
        raise NotImplementedError

    @property
    @abstractmethod
    def granted(self) -> asyncio.Event:
        """An event that is set when a reservation in the collection has been completed."""
        raise NotImplementedError

    @property
    @abstractmethod
    def rejected(self) -> asyncio.Event:
        """An event that is set when a reservation in the collection has been rejected."""
        raise NotImplementedError

    @property
    @abstractmethod
    def deadlocked(self) -> asyncio.Event:
        """An event that is set when a reservation in the collection has been deadlocked."""
        raise NotImplementedError
    
    @abstractmethod
    def clear(self) -> None:
        """Clears the reservation collection, resetting all events and states."""
        raise NotImplementedError


class IThreadReservationCoordinator(ABC):
    @abstractmethod
    async def submit_reservation_request(self, thread_id: str, request: IReservationCollection) -> None:
        raise NotImplementedError
    
    @abstractmethod
    async def start_tick_loop(self, tick_interval: float) -> None:
        """Starts the tick loop for the reservation coordinator."""
        raise NotImplementedError