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
        """Increment the starvation score for a thread."""
        self._starvation_scores[thread_id] = self._starvation_scores.get(thread_id, 0) + 1

    def get_starvation_score(self, thread_id: str) -> int:
        """Get the current starvation score for a thread."""
        return self._starvation_scores.get(thread_id, 0)

    def reset_starvation_score(self, thread_id: str) -> None:
        """Reset the starvation score for a thread to zero."""
        if thread_id in self._starvation_scores:
            self._starvation_scores[thread_id] = 0

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
    def __init__(self, thread_registry: IThreadRegistry, starvation_registry: DeadlockStarvationRegistry) -> None:
        self._thread_registry = thread_registry
        self._starvation_registry = starvation_registry

    def detect_deadlocks(
        self,
        queue: List[IReservationCollection],
    ) -> None:
        graph = self._build_wait_for_graph(queue)
        cycling_thread_ids = self._get_cycling_thread_ids(graph)

        if not cycling_thread_ids:
            return

        # Select single thread to yield based on priority
        yielding_thread_id = self._select_yielding_thread(cycling_thread_ids)

        # Mark only the selected thread as deadlocked
        for collection in queue:
            if collection.thread_id == yielding_thread_id:
                collection.rejected.clear()
                collection.deadlocked.set()
                self._starvation_registry.increment_starvation_score(collection.thread_id)
                # Note: Other cycling threads remain rejected but NOT deadlocked
                # They will retry next tick and should succeed once yielding thread clears path

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

    def _select_yielding_thread(self, cycling_thread_ids: Set[str]) -> str:
        """
        Select which thread should yield in a deadlock based on priority.

        Priority rules:
        1. Thread with LOWEST starvation score yields (allows starved threads to proceed)
        2. If tied, use lexicographic thread_id for deterministic behavior

        Args:
            cycling_thread_ids: Set of thread IDs involved in deadlock cycle

        Returns:
            Thread ID that should yield (move to parking pad)
        """
        # Get starvation scores for all cycling threads
        thread_scores = {
            thread_id: self._starvation_registry.get_starvation_score(thread_id)
            for thread_id in cycling_thread_ids
        }

        # Find minimum starvation score
        min_starvation = min(thread_scores.values())

        # Get all threads with minimum score
        candidates = [
            thread_id
            for thread_id, score in thread_scores.items()
            if score == min_starvation
        ]

        # Tie-breaker: lexicographic ordering for deterministic selection
        return sorted(candidates)[0]

    def _get_labware_to_thread_map(self, queue: List[IReservationCollection]) -> Dict[str, str]:
        """
        Build a mapping from labware IDs to thread IDs for deadlock detection.
        Handles missing threads and None labware gracefully.
        """
        labware_to_thread = {}
        for collection in queue:
            thread = self._thread_registry.get_thread(collection.thread_id)
            if thread is None:
                # Thread not found in registry - log warning and skip
                import logging
                logging.getLogger("orca").warning(
                    f"Thread {collection.thread_id} not found in registry during deadlock detection"
                )
                continue
            if thread.labware is None:
                # Thread has no labware - log warning and skip
                import logging
                logging.getLogger("orca").warning(
                    f"Thread {collection.thread_id} has no labware during deadlock detection"
                )
                continue
            labware_id = thread.labware.id
            if labware_id in labware_to_thread:
                # Duplicate labware ID - this shouldn't happen but log it
                import logging
                logging.getLogger("orca").warning(
                    f"Duplicate labware ID {labware_id} for threads "
                    f"{labware_to_thread[labware_id]} and {collection.thread_id}"
                )
            labware_to_thread[labware_id] = collection.thread_id
        return labware_to_thread
