from typing import Dict, List, Set

import networkx as nx # type: ignore

from orca.system.reservation_manager.interfaces import IReservationCollection
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.thread_registry_interface import IThreadRegistry

class DeadlockStarvationRegistry:
    """Maintains a registry to track plate movement frequency for deadlock resolution."""
    def __init__(self) -> None:
        self._starvation_scores: dict[str, int] = {}

    def increment_starvation_score(self, thread_id: str) -> None:
        """Increment the starvation score for a labware."""
        self._starvation_scores[thread_id] = self._starvation_scores.get(labware_id, 0) + 1

    def get_starvation_score(self, thread_id: str) -> int:
        """Get the current starvation score for a labware."""
        return self._starvation_scores.get(thread_id, 0)

class DeadlockGraph:
    def __init__(self) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()

    def _add_edge(self, requester: str, holder: str) -> None:
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

    def find_cycle_nodes(self) -> set[str]:
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
        queue: List[IReservationCollection],
    ) -> None:
        graph = self._build_wait_for_graph(queue)
        cycling_thread_ids = self._get_cycling_thread_ids(graph)

        for collection in queue:
            if collection.thread_id in cycling_thread_ids:
                collection.rejected.clear()
                collection.deadlocked.set()

    def _build_wait_for_graph(
        self,
        queue: List[IReservationCollection],
    ) -> DeadlockGraph:
        graph = DeadlockGraph()
        labwares_in_queue = self._get_labware_to_thread_map(queue)
        for collection in queue:
            requesting_thread_id = collection.thread_id

            for reservation in collection.get_reservations():
                blocking_thread_id = self._get_blocking_thread_id(reservation, labwares_in_queue)
                if blocking_thread_id:
                    graph._add_edge(requesting_thread_id, blocking_thread_id)

        return graph

    def _get_blocking_thread_id(
        self,
        reservation: LocationReservation,
        labwares_in_queue: Dict[str, str],
    ) -> str | None:
        
        requested_location = reservation.requested_location
        blocking_labware = requested_location.labware
        if blocking_labware is None:
            return None
        blocking_thread_id = labwares_in_queue.get(blocking_labware.id)
        return blocking_thread_id


    def _get_cycling_thread_ids(self, graph: DeadlockGraph) -> Set[str]:
        return graph.find_cycle_nodes()
    
    def _get_labware_to_thread_map(self, queue: List[IReservationCollection]) -> Dict[str, str]:
        return {self._thread_registry.get_thread(c.thread_id).labware.id: c.thread_id for c in queue}
