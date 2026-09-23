import logging
from typing import Callable, Dict, List, Sequence, Set

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.errors import UnresolvableDeadlockContext
from orca.system.reservation_manager.interfaces import IReservationCollection
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    outranks,
)
from orca.system.thread_registry_interface import IThreadRegistry

orca_logger = logging.getLogger("orca")

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

class ThreadDeadlockDetector:
    def __init__(
        self,
        thread_registry: IThreadRegistry,
        starvation_registry: DeadlockStarvationRegistry,
        reservation_at: Callable[[str], LocationReservation | None],
        owned_sites_of: Callable[[str], List[Location]] | None = None,
        exclusion_siblings_of: Callable[[str], List[Location]] | None = None,
    ) -> None:
        self._thread_registry = thread_registry
        self._starvation_registry = starvation_registry
        # Read-only holder lookup: the detector observes reservations, never
        # mutates them. Required, so a new call site cannot silently miss a blocker.
        self._reservation_at = reservation_at
        # Flat model keeps plates on child sites, so a mutex candidate's own
        # labware is always None; site occupants must cast the blocker edges.
        self._owned_sites_of = owned_sites_of
        # Single-occupancy carriages: a candidate can be blocked by a SIBLING
        # position's occupant/holder even while the candidate itself is empty.
        self._exclusion_siblings_of = exclusion_siblings_of
        self._rejected_carry: dict[str, IReservationCollection] = {}
        self._deadlocked_threads: set[str] = set()

    def forget_threads(self, thread_ids: set[str]) -> None:
        """Drop all per-thread bookkeeping for threads that no longer exist.

        Cross-tick detection carries a thread's last rejected collection (with
        live Location refs) across ticks; for an aborted thread that carry would
        feed a phantom cycle into a later execution. Clears the carry, the
        flagged-deadlock membership, and the starvation score.
        """
        for thread_id in thread_ids:
            self._rejected_carry.pop(thread_id, None)
            self._deadlocked_threads.discard(thread_id)
            self._starvation_registry.reset_starvation_score(thread_id)

    def find_yielding_thread(
        self,
        queue: Sequence[IReservationCollection],
    ) -> str | None:
        """Detect a deadlock and return the thread that should yield.

        Pure analysis: computes the stuck set by AND-OR knot detection and
        selects the yielding thread (lowest starvation score). Does NOT modify
        any collection state.

        Returns:
            Thread ID that should yield, or None if no thread is deadlocked.
        """
        stuck = self._compute_stuck_set(queue)
        if not stuck:
            return None
        return self._select_yielding_thread(stuck)

    def find_unresolvable_blocker(
        self,
        collection: IReservationCollection,
    ) -> UnresolvableDeadlockContext | None:
        """Per-collection check for an out-of-queue immovable blocker.

        Round 1 of S3 deadlock detection. The knot detector
        (`find_yielding_thread`) computes its stuck set only over threads
        with reservation collections IN the current tick's queue.
        Threads parked on `orca.join` outside the queue are invisible
        to that path: the blocker labware is never in
        `_get_labware_to_thread_map`, so it contributes no blocker and the
        knot is never seen.

        This method covers the structural gap by consulting the full
        thread registry for the blocker's owning thread, then declaring
        an unresolvable deadlock only when that thread's template has
        `immovable=True`. The narrow gate keeps Round 1 free of
        false-positive declarations during normal slow waits.

        Works on a SINGLE rejected collection (not a queue-wide search):
        the user's repro has only one thread in queue (the entry thread);
        the blocker is parked on `orca.join` outside.

        Returns the first immovable-blocker context found across all
        rejection candidates, or None.
        """
        if not collection.rejected.is_set():
            return None

        # `get_thread` raises KeyError in production registries (returns None
        # only in test mocks). Treat either signal as "skip" -- a missing
        # requester is a transient state, not a crash candidate.
        try:
            requesting_thread = self._thread_registry.get_thread(collection.thread_id)
        except KeyError:
            return None
        if requesting_thread is None or requesting_thread.labware is None:
            return None

        for reservation in collection.get_reservations():
            location = reservation.requested_location
            # Iterate ALL candidates -- multi-slot devices may hold multiple
            # labwares concurrently, and this immovable check must see each.
            candidates: List[LabwareInstance] = []
            if location.labware is not None:
                candidates.append(location.labware)
            candidates.extend(location.loaded_labware)
            for blocker in candidates:
                try:
                    blocking_thread = self._thread_registry.get_thread_by_labware(blocker.id)
                except KeyError:
                    # Labware not owned by any thread (transient state, e.g.
                    # mid-handoff). Not a deadlock candidate; skip.
                    continue
                template = blocking_thread.thread_template
                if template is None or not template.immovable:
                    continue
                return UnresolvableDeadlockContext(
                    requesting_thread_id=collection.thread_id,
                    requesting_labware_id=requesting_thread.labware.id,
                    blocking_position_id=location.position_id,
                    blocking_thread_id=blocking_thread.id,
                    blocking_labware_id=blocker.id,
                    reason="blocking thread declared immovable=True",
                    hint=(
                        f"Set immovable=False on '{blocking_thread.name}' "
                        f"if the labware can be moved by other threads, or use "
                        f"start=(<loc>, REUSE_EXISTING) for deck-resident reagents "
                        f"that participate in actions as co-inputs."
                    ),
                )
        return None

    def detect_deadlocks(
        self,
        queue: Sequence[IReservationCollection],
    ) -> None:
        """Detect a deadlock and mark the yielding thread's collection."""
        yielding_thread_id = self.find_yielding_thread(queue)
        if yielding_thread_id is None:
            return

        for collection in queue:
            if collection.thread_id == yielding_thread_id:
                collection.rejected.clear()
                collection.deadlocked.set()
                self._starvation_registry.increment_starvation_score(collection.thread_id)

    def is_flagged_for_deadlock(self, thread_id: str) -> bool:
        """Check and consume a cross-tick deadlock flag.

        Returns True if the thread was flagged from a previous tick's
        carry-based detection.  The flag is consumed (removed) so the
        thread is only marked once.
        """
        if thread_id in self._deadlocked_threads:
            self._deadlocked_threads.discard(thread_id)
            self._rejected_carry.pop(thread_id, None)
            self._starvation_registry.increment_starvation_score(thread_id)
            return True
        return False

    def process_tick_results(self, collections: Sequence[IReservationCollection]) -> None:
        """Post-processing after the coordinator attempts all reservations.

        1. Same-tick deadlock detection on rejected collections (marks directly).
        2. Update carry: add rejections, remove grants and deadlocks.
        3. Cross-tick deadlock detection from accumulated carry (flags threads).
        4. Reset starvation scores for granted threads.
        """
        # Same-tick detection (marks the yielding collection directly)
        rejected = [c for c in collections if c.rejected.is_set()]
        if rejected:
            self.detect_deadlocks(rejected)

        # Update carry
        for collection in collections:
            if collection.granted.is_set() or collection.deadlocked.is_set():
                self._rejected_carry.pop(collection.thread_id, None)
            elif collection.rejected.is_set():
                self._rejected_carry[collection.thread_id] = collection

        # Reset on REAL progress only: action-acquisition grants reset here;
        # move collections defer to arrival (MoveHandler._mark_episode_escaped).
        for collection in collections:
            if collection.granted.is_set() and collection.resets_starvation_on_grant:
                self._starvation_registry.reset_starvation_score(collection.thread_id)

        # Cross-tick detection from carry
        self._detect_cross_tick_deadlock()

    def get_starvation_score(self, thread_id: str) -> int:
        """Delegate to the starvation registry."""
        return self._starvation_registry.get_starvation_score(thread_id)

    def _detect_cross_tick_deadlock(self) -> None:
        """Detect deadlocks from collections accumulated across multiple ticks.

        Stale collections in carry still have valid ``requested_location``
        references (``clear()`` only resets event flags), and the thread
        registry provides live labware positions.  When a cycle is found
        the yielding thread is *flagged* rather than marked directly,
        because the carry's collection object is stale.  The flag is
        consumed on the thread's next submission via ``is_flagged_for_deadlock``.
        """
        carry_list = list(self._rejected_carry.values())
        if len(carry_list) < 2:
            return

        yielding_thread_id = self.find_yielding_thread(carry_list)
        if yielding_thread_id is None:
            return

        orca_logger.info(
            f"Cross-tick deadlock detected: flagging thread {yielding_thread_id}"
        )
        self._deadlocked_threads.add(yielding_thread_id)
        self._rejected_carry.pop(yielding_thread_id, None)

    def _compute_stuck_set(self, queue: Sequence[IReservationCollection]) -> Set[str]:
        """The set of threads in a deadlock (AND-OR knot), by greatest fixpoint.

        A reservation collection is a pick-ONE (OR) wait: the thread proceeds if
        ANY candidate frees. A candidate frees once ALL its blockers release, and
        a blocker releases iff its holder is not itself stuck. So a set S is a
        deadlock iff every thread in S has every candidate blocked by a thread in
        S. Compute S as the greatest fixpoint: seed it with every waiting thread,
        then repeatedly remove any thread with an ESCAPABLE candidate -- one whose
        blockers are all outside S (a free candidate, or one held only by threads
        that will make progress) -- until stable.

        This replaces plain digraph cycle detection, which cannot express OR-wait:
        it added an edge per candidate, so a free alternative on each side still
        formed a cycle (false positive, force-parking a live thread), and a real
        knot spanning more than one simple cycle went undetected (false negative).
        """
        labwares_in_queue = self._get_labware_to_thread_map(queue)
        blockers_by_thread: Dict[str, List[Set[str]]] = {}
        for collection in queue:
            thread_id = collection.thread_id
            blockers_by_thread[thread_id] = [
                self._blocker_thread_ids(reservation, labwares_in_queue, thread_id)
                for reservation in collection.get_reservations()
            ]

        stuck = set(blockers_by_thread.keys())
        changed = True
        while changed:
            changed = False
            for thread_id in list(stuck):
                candidates = blockers_by_thread[thread_id]
                # Escapable = requests nothing, or SOME candidate's blockers are
                # all outside S (an empty blocker set is a free candidate).
                escapable = not candidates or any(
                    not (blockers & stuck) for blockers in candidates
                )
                if escapable:
                    stuck.discard(thread_id)
                    changed = True
        return stuck

    def _blocker_thread_ids(
        self,
        reservation: LocationReservation,
        labwares_in_queue: Dict[str, str],
        requesting_thread_id: str,
    ) -> Set[str]:
        """Every thread whose hold blocks this candidate, EXCLUDING the requester.

        A candidate can be blocked several ways at once (a foreign mutex holder
        AND a foreign plate resident on the site), and it only frees when ALL of
        them clear -- so the deadlock test needs the full blocker SET, not a
        single mutex-first pick. Reservation holders come first (owner mutex, then
        the position itself, mirroring ``can_reserve``); the occupancy fallback
        catches an in-queue resident plate a bridge exposes via ``loaded_labware``.
        A ``thread_id=None`` system hold yields no blocker (rule 5 stall-detector
        territory), and a candidate the requester itself holds is not a block
        (re-entrant grant), so both are dropped. Nor is a hold at the requested
        position that this request outranks: the gate would grant over it, so
        calling it a blocker would have the detector waiting on a thread
        nothing is waiting for. The mutex holder and the carriage siblings are
        NOT ranked, because the gate does not rank them either.
        """
        blockers: Set[str] = set()
        location = reservation.requested_location
        mutex_key = location.owner_mutex_id
        if mutex_key is not None:
            holder = self._reservation_at(mutex_key)
            if holder is not None and holder.thread_id is not None:
                blockers.add(holder.thread_id)
        blocker = self._blocking_thread(
            reservation, self._reservation_at(location.position_id),
        )
        if blocker is not None:
            blockers.add(blocker)

        candidates: List[LabwareInstance] = []
        if location.labware is not None:
            candidates.append(location.labware)
        candidates.extend(location.loaded_labware)
        if self._owned_sites_of is not None:
            for site in self._owned_sites_of(location.position_id):
                if site.labware is not None:
                    candidates.append(site.labware)
                candidates.extend(site.loaded_labware)
        if self._exclusion_siblings_of is not None:
            # Mirrors the reservation gate: without sibling edges a
            # sibling-blocked waiter reads escapable and bridge knots vanish.
            for sibling in self._exclusion_siblings_of(location.position_id):
                if sibling.labware is not None:
                    candidates.append(sibling.labware)
                holder = self._reservation_at(sibling.position_id)
                if holder is not None and holder.thread_id is not None:
                    blockers.add(holder.thread_id)
        for blocking_labware in candidates:
            blocking_thread_id = labwares_in_queue.get(blocking_labware.id)
            if blocking_thread_id is not None:
                blockers.add(blocking_thread_id)

        blockers.discard(requesting_thread_id)
        return blockers

    @staticmethod
    def _blocking_thread(
        request: LocationReservation, holder: LocationReservation | None,
    ) -> str | None:
        """The thread this holder makes the request wait for, or None.

        The same test the reservation gate makes at this position, so the
        detector cannot report a wait the gate would not impose. A hold with no
        owning thread is nobody to wait for."""
        if holder is None or holder.thread_id is None:
            return None
        if outranks(request.priority, holder.priority):
            return None
        return holder.thread_id

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

    def _get_labware_to_thread_map(self, queue: Sequence[IReservationCollection]) -> Dict[str, str]:
        """
        Build a mapping from labware IDs to thread IDs for deadlock detection.
        Handles missing threads and None labware gracefully.
        """
        labware_to_thread = {}
        for collection in queue:
            thread = self._thread_registry.get_thread(collection.thread_id)
            if thread is None:
                orca_logger.warning(
                    f"Thread {collection.thread_id} not found in registry during deadlock detection"
                )
                continue
            if thread.labware is None:
                orca_logger.warning(
                    f"Thread {collection.thread_id} has no labware during deadlock detection"
                )
                continue
            labware_id = thread.labware.id
            if labware_id in labware_to_thread:
                orca_logger.warning(
                    f"Duplicate labware ID {labware_id} for threads "
                    f"{labware_to_thread[labware_id]} and {collection.thread_id}"
                )
            labware_to_thread[labware_id] = collection.thread_id
        return labware_to_thread
