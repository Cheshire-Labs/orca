
from abc import abstractmethod
import asyncio
import logging
from typing import Dict, List, Optional, Set
import uuid
import networkx as nx # type: ignore
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import ILabwareLocationObserver, Location
from orca.system.system_map import ILocationRegistry
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.status_enums import LabwareThreadStatus

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

class LocationReservation: 
    def __init__(self, requested_location: Location, labware: LabwareInstance | None = None) -> None:
        self._id = str(uuid.uuid4())
        self._labware = labware
        self._requested_location: Location = requested_location
        self._reserved_location: Optional[Location] = None
        self._reservation_manager: IReservationManager | None = None
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

    # def release_reservation(self) -> None:
    #     if self._reservation_manager is None:
    #         raise ValueError("Reservation manager not set")
    #     self._reservation_manager.release_reservation(self.reserved_location.name)
class IReservationCollection:
    @abstractmethod
    def get_reservations(self) -> List[LocationReservation]:
        """Returns a list of all reservations."""
        raise NotImplementedError
    
    @abstractmethod
    def resolve_final_reservation(self, reservation_manager: IReservationManager) -> None:
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
    

class IThreadReservationCoordinator:

    async def submit_reservation_request(self, thread_id: str, request: IReservationCollection) -> None:
        raise NotImplementedError


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
        request.granted.set()
        request.processed.set()
        orca_logger.info(f"Thread {request.labware} - Reservation {request.id} granted for {location_name}")

    def can_reserve(self, location_name: str) -> bool:
        return location_name not in self._reservations.keys() and self._location_reg.get_location(location_name).labware is None
    
    def release_reservation(self, location_name: str) -> None:
        if location_name in self._reservations.keys():
            reservation = self._reservations[location_name]
            orca_logger.info(f"Releasing reservation {reservation.id} for {location_name}")
            del self._reservations[location_name]



class ThreadReservationCoordinator(IThreadReservationCoordinator, IAvailabilityManager, ILabwareLocationObserver):
    def __init__(self, location_reg: ILocationRegistry, thread_registry: IThreadRegistry) -> None:
        self._location_reg = location_reg
        self._reservation_manager: LocationReservationManager = LocationReservationManager(location_reg)
        self._queue: Dict[str, IReservationCollection] = {}
        # self._deadlock_detector = DeadlockDetector(location_reg, self._location_reservations, self._location_queues)
        self._deadlock_detector = ThreadDeadlockDetector(thread_registry)
        self._lock = asyncio.Lock()

    async def start_tick_loop(self, tick_interval: float = 0.2) -> None:
        """Starts a periodic tick loop to check for deadlocks and process reservations."""
        while True:
            await asyncio.sleep(tick_interval)
            await self._on_tick()

    async def _on_tick(self) -> None:
        """This method is called periodically to check for deadlocks and process reservations."""
        
        # copy over the queue
        async with self._lock:
            queue_snapshot = dict(self._queue)
            self._queue.clear()

        # attempt to get a reservation
        for _, collection in queue_snapshot.items():
            for r in collection.get_reservations():
                self._reservation_manager.attempt_reservation(r.requested_location.name, r)
            
            collection.resolve_final_reservation(self._reservation_manager)

        self._detect_dead_lock(queue_snapshot)

    def _detect_dead_lock(self, queue: Dict[str, IReservationCollection]) -> None:
        """Detects deadlocks in the current reservation state."""
        
        rejected_queue = {thread_id: c for thread_id, c in queue.items() if c.rejected.is_set()}
        if not rejected_queue:
            return
 
        self._deadlock_detector.detect_deadlocks(rejected_queue, self._reservation_manager.reservations)

        
    async def submit_reservation_request(self, thread_id: str, request: IReservationCollection) -> None:
        async with self._lock:
            self._queue[thread_id] = request









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




class DeadlockGraph:
    def __init__(self) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()
        
    def _add_edge(self, requester: LabwareInstance, holder: LabwareInstance) -> None:
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
        
    def find_cycle_nodes(self) -> set[LabwareInstance]:
        try:
            cycle = nx.find_cycle(self._graph, orientation='original') # type: ignore
            return {node for node, _, _ in cycle}
        except nx.NetworkXNoCycle:
            return set()

class ThreadDeadlockDetector:
    def __init__(self, thread_registry: IThreadRegistry) -> None:
        self._thread_registry = thread_registry

    def detect_deadlocks(
        self,
        queue: Dict[str, IReservationCollection],
        location_reservations: Dict[str, LocationReservation]
    ) -> None:
        graph = self._build_wait_for_graph(queue, location_reservations)
        cycling_labware = self._get_cycling_labware(graph)

        for thread_id, collection in queue.items():
            if self._is_thread_deadlocked(collection, location_reservations, cycling_labware):
                collection.deadlocked.set()
                collection.processed.set()

    def _build_wait_for_graph(
        self,
        queue: Dict[str, IReservationCollection],
        location_reservations: Dict[str, LocationReservation]
    ) -> DeadlockGraph:
        graph = DeadlockGraph()

        for thread_id, collection in queue.items():
            requester = self._thread_registry.get_thread(thread_id).labware

            for reservation in collection.get_reservations():
                blocker = self._get_blocking_labware(reservation, location_reservations)
                if blocker:
                    graph._add_edge(requester, blocker)

        return graph


    def _is_thread_deadlocked(
        self,
        collection: IReservationCollection,
        location_reservations: Dict[str, LocationReservation],
        cycle_labware: Set[LabwareInstance]
    ) -> bool:
        for reservation in collection.get_reservations():
            blocker = self._get_blocking_labware(reservation, location_reservations)
            if blocker is None or blocker not in cycle_labware:
                return False

        return True
    
    def _get_blocking_labware(
        self,
        reservation: LocationReservation,
        location_reservations: Dict[str, LocationReservation]
    ) -> Optional[LabwareInstance]:
        blocking_res = location_reservations.get(reservation.requested_location.name)
        return blocking_res.labware if blocking_res else None

    def _get_cycling_labware(self, graph: DeadlockGraph) -> Set[LabwareInstance]:
        return graph.find_cycle_nodes()

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