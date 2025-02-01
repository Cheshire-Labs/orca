
import asyncio
import logging
from typing import Dict, List, Optional
import uuid
import networkx as nx # type: ignore
from orca.resource_models.labware import Labware
from orca.resource_models.location import ILabwareLocationObserver, Location
from orca.system.system_map import ILocationRegistry

orca_logger = logging.getLogger("orca")

class ILabwareLocationManager:
    def is_occupied(self, location: Location) -> bool:
        raise NotImplementedError

class IAvailabilityManager:
    def is_location_available(self, location: Location) -> bool:
        raise NotImplementedError
    
    async def await_available(self, location: Location) -> None:
        raise NotImplementedError


class IReservationManager:
    def can_reserve(self, location_name: str) -> bool:
        raise NotImplementedError
    
    def release_reservation(self, location_name: str) -> None:
        raise NotImplementedError

    def request_reservation(self, location_name: str, reservation: "LocationReservation") -> None:
        raise NotImplementedError


class LocationReservation: 
    def __init__(self, requested_location: Location, labware: Labware | None = None) -> None:
        self._id = str(uuid.uuid4())
        self._labware = labware
        self._requested_location: Location = requested_location
        self._reserved_location: Optional[Location] = None
        self._reservation_manager: IReservationManager | None = None
        self.completed: asyncio.Event  = asyncio.Event()
        self.deadlocked: asyncio.Event = asyncio.Event()
        self.cancelled: asyncio.Event = asyncio.Event()

    @property
    def id(self) -> str:
        return self._id
    
    @property
    def labware(self) -> Labware | None:
        return self._labware

    def set_location(self, location: Location) -> None:
        if self.cancelled.is_set():
            raise ValueError("Reservation has been cancelled")
        self._reserved_location = location
        self.completed.set()
    
    @property
    def requested_location(self) -> Location:
        return self._requested_location

    @property
    def reserved_location(self) -> Location:
        if self._reserved_location is None:
            raise ValueError("Location not yet reserved")
        return self._reserved_location

    def request_reservation(self, reservation_manager: IReservationManager) -> None:
        self._reservation_manager = reservation_manager
        reservation_manager.request_reservation(self._requested_location.name, self) 

    def release_reservation(self) -> None:
        if self._reservation_manager is None:
            raise ValueError("Reservation manager not set")
        self._reservation_manager.release_reservation(self.reserved_location.name)
    
class ReservationManager(IReservationManager, IAvailabilityManager, ILabwareLocationObserver):
    def __init__(self, location_reg: ILocationRegistry) -> None:
        self._location_reg = location_reg
        self._location_reservations: Dict[str, LocationReservation] = {}
        self._location_queues: Dict[str, List[LocationReservation]] = {}
        self._deadlock_detector = DeadlockDetector(location_reg, self._location_reservations, self._location_queues)

    def request_reservation(self, location_name: str, request: LocationReservation) -> None:
        self._location_reg.get_location(location_name).add_observer(self)
        self._add_to_waitlist(location_name, request)
        self._process_next_request(location_name)

    def _reserve(self, location_name: str, request: LocationReservation) -> None:
        self._location_reservations[location_name] = request
        request.set_location(self._location_reg.get_location(location_name))
        request.completed.set()
        orca_logger.info(f"Thread {request.labware} - Reservation {request.id} granted for {location_name}")

    def _process_next_request(self, location_name: str) -> None:
        queue = self._location_queues.setdefault(location_name, [])
        if len(queue) == 0:
            return
        if self.can_reserve(location_name):
            request = queue.pop(0)
            self._reserve(location_name, request)
        else:
            if self._deadlock_detector.is_deadlocked():
                self._handle_deadlock(queue.pop(0))
            

    def _add_to_waitlist(self, location_name: str, request: LocationReservation) -> None:
        queue = self._location_queues.setdefault(location_name, [])
        if request not in queue:
            queue.append(request)
            orca_logger.info(f"Thread {request.labware} - Reservation {request.id} waiting for availability at {location_name}")
    
    def can_reserve(self, location_name: str) -> bool:
        return location_name not in self._location_reservations.keys() and self._location_reg.get_location(location_name).labware is None

    def release_reservation(self, location_name: str) -> None:
        if location_name in self._location_reservations.keys():
            reservation = self._location_reservations[location_name]
            orca_logger.info(f"Releasing reservation {reservation.id} for {location_name}")
            del self._location_reservations[location_name]
            self._process_next_request(location_name)
            

    def notify_labware_location_change(self, event: str, location: Location, labware: Labware) -> None:
        if event == "picked":
            if self.can_reserve(location.name):
                self._process_next_request(location.name)

    def _handle_deadlock(self, request: LocationReservation):
        orca_logger.info("Deadlock detected")
        request.deadlocked.set()
        request.completed.set()


class DeadlockDetector:
    def __init__(self, location_registry: ILocationRegistry, location_reservations: Dict[str, LocationReservation], reservation_queues: Dict[str, List[LocationReservation]]) -> None:
        self._location_registry = location_registry
        self._location_reservations = location_reservations
        self._reservation_queues = reservation_queues
    
    def is_deadlocked(self) -> bool:
        graph = DeadlockGraph()
        for location_name, queue in self._reservation_queues.items():
            for request in queue:
                requestor = request.labware
                holder = self._location_registry.get_location(location_name).labware
                if holder is None or requestor is None:
                    continue
                graph._add_edge(requestor, holder)

        return graph.is_deadlocked()

class DeadlockGraph:
    def __init__(self) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()
        
    def _add_edge(self, requester: Labware, holder: Labware) -> None:
        if not self._graph.has_node(requester): # type: ignore
            self._graph.add_node(requester) # type: ignore
        if not self._graph.has_node(holder): # type: ignore
            self._graph.add_node(holder) # type: ignore
        self._graph.add_edge(requester, holder) # type: ignore
        
    def reset(self) -> None:
        self._graph.clear() # type: ignore

    def is_deadlocked(self) -> bool:
        try:
            cycle = nx.find_cycle(self._graph, orientation='original') # type: ignore
            return True
        except nx.NetworkXNoCycle:
            return False




