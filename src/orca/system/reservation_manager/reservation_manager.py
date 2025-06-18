
import asyncio
import logging
from typing import Dict, List
from orca.resource_models.location import ILabwareLocationObserver
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.deadlock_manager import ThreadDeadlockDetector
from orca.system.reservation_manager.interfaces import IAvailabilityManager, IReservationCollection, IReservationManager, IThreadReservationCoordinator
from orca.system.system_map import ILocationRegistry
from orca.system.thread_registry_interface import IThreadRegistry

orca_logger = logging.getLogger("orca")

class LocationReservationManager(IReservationManager, IAvailabilityManager, ILabwareLocationObserver):
    def __init__(self, location_reg: ILocationRegistry) -> None:
        self._location_reg = location_reg
        self._reservations: Dict[str, LocationReservation] = {}

    @property
    def reservations(self) -> Dict[str, LocationReservation]:
        """Returns the current reservations."""
        return self._reservations

    def get_reservation_at(self, location_name: str) -> LocationReservation | None:
        """Returns the reservation for the given location name, if it exists."""
        return self._reservations.get(location_name, None)

    def attempt_reservation(self, location_name: str, request: LocationReservation) -> None:
        """Attempts to reserve a location for the given request."""
        if self.can_reserve(location_name):
            self._reserve(location_name, request)
            request.granted.set()
        else:
            request.rejected.set()
        request.processed.set()

    def _reserve(self, location_name: str, request: LocationReservation) -> None:
        self._reservations[location_name] = request
        request.set_location(self._location_reg.get_location(location_name))
        request.set_reservation_release_callback(lambda: self.release_reservation(location_name))
        orca_logger.info(f"Thread {request.labware} - Reservation {request.id} granted for {location_name}")

    def can_reserve(self, location_name: str) -> bool:
        loation_is_unreserved = location_name not in self._reservations.keys() 
        location_is_empty = self._location_reg.get_location(location_name).labware is None
        return loation_is_unreserved and location_is_empty
    
    def release_reservation(self, location_name: str) -> None:
        if location_name in self._reservations.keys():
            reservation = self._reservations[location_name]
            orca_logger.info(f"Releasing reservation {reservation.id} for {location_name}")
            del self._reservations[location_name]



class ThreadReservationCoordinator(IThreadReservationCoordinator, IAvailabilityManager, ILabwareLocationObserver):
    def __init__(self, location_reg: ILocationRegistry, thread_registry: IThreadRegistry) -> None:
        self._location_reg = location_reg
        self._reservation_manager: LocationReservationManager = LocationReservationManager(location_reg)
        self._queue: List[IReservationCollection] = []
        # self._deadlock_detector = DeadlockDetector(location_reg, self._location_reservations, self._location_queues)
        self._deadlock_detector = ThreadDeadlockDetector(thread_registry)
        self.ticker_started = False
        self._lock = asyncio.Lock()

    async def start_tick_loop(self, tick_interval: float = 0.3) -> None:
        """Starts a periodic tick loop to check for deadlocks and process reservations."""
        self.ticker_started = True
        while True:
            await asyncio.sleep(tick_interval)
            await self._on_tick()

    async def _on_tick(self) -> None:
        """This method is called periodically to check for deadlocks and process reservations."""
        
        # copy over the queue
        async with self._lock:
            queue_snapshot = list(self._queue)
            self._queue.clear()

        # attempt to get a reservation
        for collection in queue_snapshot:
            for r in collection.get_reservations():
                self._reservation_manager.attempt_reservation(r.requested_location.name, r)
            
            collection.resolve_final_reservation()
            collection.processed.set()

        self._detect_dead_lock(queue_snapshot)

    def _detect_dead_lock(self, queue: List[IReservationCollection]) -> None:
        """Detects deadlocks in the current reservation state."""
        
        rejected_queue = [c for c in queue if c.rejected.is_set()]
        if not rejected_queue:
            return
 
        self._deadlock_detector.detect_deadlocks(rejected_queue)

        
    async def submit_reservation_request(self, thread_id: str, request: IReservationCollection) -> None:
        if self.ticker_started is False:
            orca_logger.warning("Reservation Coordinator Ticker not started.")
        async with self._lock:
            self._queue.append(request)









# class ReservationManager(IReservationManager, IAvailabilityManager, ILabwareLocationObserver):
#     def __init__(self, location_reg: ILocationRegistry) -> None:
#         self._location_reg = location_reg
#         self._location_reservations: Dict[str, LocationReservation] = {}
#         self._location_queues: Dict[str, List[LocationReservation]] = {}
#         self._deadlock_detector = DeadlockDetector(location_reg, self._location_reservations, self._location_queues)

#     def submit_reservation_request(self, location_name: str, request: LocationReservation) -> None:
#         self._location_reg.get_location(location_name).add_observer(self)
#         queue = self._location_queues.setdefault(location_name, [])
#         if request not in queue:
#             queue.append(request)
#             orca_logger.info(f"Thread {request.labware} - Reservation {request.id} waiting for availability at {location_name}")

#     def _reserve(self, location_name: str, request: LocationReservation) -> None:
#         self._location_reservations[location_name] = request
#         request.set_location(self._location_reg.get_location(location_name))
#         request.granted.set()
#         request.processed.set()
#         orca_logger.info(f"Thread {request.labware} - Reservation {request.id} granted for {location_name}")

#     def _process_next_request(self, location_name: str) -> None:
#         queue = self._location_queues.setdefault(location_name, [])
#         if len(queue) == 0:
#             return
#         if self.can_reserve(location_name):
#             request = queue.pop(0)
#             self._reserve(location_name, request)
#         else:
#             if self._deadlock_detector.is_deadlocked():
#                 self._handle_deadlock(queue.pop(0))
    
#     def can_reserve(self, location_name: str) -> bool:
#         return location_name not in self._location_reservations.keys() and self._location_reg.get_location(location_name).labware is None

#     def release_reservation(self, location_name: str) -> None:
#         if location_name in self._location_reservations.keys():
#             reservation = self._location_reservations[location_name]
#             orca_logger.info(f"Releasing reservation {reservation.id} for {location_name}")
#             del self._location_reservations[location_name]
#             self._process_next_request(location_name)
            

#     def notify_labware_location_change(self, event: str, location: Location, labware: LabwareInstance) -> None:
#         if event == "picked":
#             if self.can_reserve(location.name):
#                 self._process_next_request(location.name)

#     def _handle_deadlock(self, request: LocationReservation):
#         orca_logger.info("Deadlock detected")
#         request.deadlocked.set()
#         request.processed.set()




# class ThreadDeadlockDetector:
#     def __init__(self, thread_registry: IThreadRegistry) -> None:
#         self._thread_registry = thread_registry

        


#     def is_deadlocked(self, queue: Dict[str, IReservationCollection], location_reservations: Dict[str, LocationReservation]) -> bool:
#         deadlock_graph = self._build_wait_for_graph(queue, location_reservations)


        
#         for thread_id, reservation_collection in queue.items():
#             thread = self._thread_registry.get_thread(thread_id)
#             requesting_labware = thread.labware
#             collection_is_deadlocked = self.is_collection_deadlocked(requesting_labware, reservation_collection, location_reservations)
#             if collection_is_deadlocked:
#                 reservation_collection.deadlocked.set()

#     def _build_wait_for_graph(
#         self,
#         queue: Dict[str, IReservationCollection],
#         location_reservations: Dict[str, LocationReservation]
#     ) -> DeadlockGraph:
#         graph = DeadlockGraph()

#         for thread_id, collection in queue.items():
#             requester = self._thread_registry.get_thread(thread_id).labware

#             for reservation in collection.get_reservations():
#                 blocker = self._get_blocking_labware(reservation, location_reservations)
#                 if blocker:
#                     graph._add_edge(requester, blocker)

#         return graph


#     def is_collection_deadlocked(self, requesting_labware: LabwareInstance, reservation_collection: IReservationCollection, location_reservations: Dict[str, LocationReservation]) -> bool:
#         graph: DeadlockGraph = DeadlockGraph()
#         # build a directed graph of labware requests
#         for reservation in reservation_collection.get_reservations():
#             request_location = reservation.requested_location
#             blocking_reservation = location_reservations.get(request_location.name)
#             blocking_labware = blocking_reservation.labware if blocking_reservation else None
#             if blocking_labware is None:
#                 continue
#             graph._add_edge(requesting_labware, blocking_labware)
#         return graph.is_deadlocked()

