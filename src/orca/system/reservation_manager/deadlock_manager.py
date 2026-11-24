import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Set

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.errors import UnresolvableDeadlockContext
from orca.system.reservation_manager.interfaces import IReservationCollection
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    outranks,
)
from orca.system.reservation_manager.thread_registry_interface import IThreadRegistry

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

@dataclass(frozen=True)
class _Deadlock:
    """The thread asked to move its plate aside, and who gets each location that frees."""
    yielding_thread_id: str
    hand_overs: Dict[str, str] = field(default_factory=dict)


class ThreadDeadlockDetector:
    def __init__(
        self,
        thread_registry: IThreadRegistry,
        starvation_registry: DeadlockStarvationRegistry,
        reservation_at: Callable[[str], LocationReservation | None],
        owned_sites_of: Callable[[str], List[Location]] | None = None,
        exclusion_siblings_of: Callable[[str], List[Location]] | None = None,
        hand_over: Callable[[str, str], None] | None = None,
        promised_to: Callable[[str], str | None] | None = None,
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
        # Promises a freed location (position_id, thread_id) to the waiter a yield is made for.
        self._hand_over = hand_over
        self._promised_to = promised_to
        self._rejected_carry: dict[str, IReservationCollection] = {}
        self._deadlocked_threads: set[str] = set()
        # Waits that never enter the queue. A thread with one is read from here, not from the carry.
        self._location_waits: dict[str, LocationReservation] = {}
        self._co_labware_waits: dict[str, Callable[[], Set[str]]] = {}
        # Threads that were told to yield and had nowhere to move their plate. Not picked again until granted something.
        self._cannot_yield: set[str] = set()

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
            self._location_waits.pop(thread_id, None)
            self._co_labware_waits.pop(thread_id, None)
            self._cannot_yield.discard(thread_id)
            self._starvation_registry.reset_starvation_score(thread_id)

    def note_location_wait(self, thread_id: str, request: LocationReservation) -> None:
        """A thread waits outside the queue for this one location: a one-candidate wait."""
        self._location_waits[thread_id] = request

    def end_location_wait(self, thread_id: str) -> None:
        self._location_waits.pop(thread_id, None)

    def note_co_labware_wait(self, thread_id: str, missing_labware_ids: Callable[[], Set[str]]) -> None:
        """A thread holds its device until every missing plate arrives. Read live, so an arrival ends the edge."""
        self._co_labware_waits[thread_id] = missing_labware_ids

    def end_co_labware_wait(self, thread_id: str) -> None:
        self._co_labware_waits.pop(thread_id, None)

    def note_cannot_yield(self, thread_id: str) -> None:
        """The thread was told to yield and could not move its plate aside. Asking it again every tick
        would never settle, and would keep the stall detector from seeing a steady state."""
        self._cannot_yield.add(thread_id)

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
        deadlock = self._find_deadlock(queue)
        return deadlock.yielding_thread_id if deadlock is not None else None

    def _find_deadlock(self, queue: Sequence[IReservationCollection]) -> _Deadlock | None:
        """The yield that breaks the deadlock among these requests and the registered waits, or None.

        Only a thread with a queued request can be told to yield. Moving its plate aside helps
        only if another stuck thread waits for a location it holds or stands on. A co-labware
        wait is not that: it waits for the plate to arrive, and moving it aside brings it no
        closer. The thread must also be on a cycle: a stuck thread that only queues behind the
        cycle frees nothing the cycle needs. A deadlock with no thread to yield is left to the
        stall detector.
        """
        blockers_by_thread = self._blockers_by_thread(queue)
        stuck = self._stuck_among(blockers_by_thread)
        in_the_way = {
            blocker
            for thread_id in stuck - self._co_labware_waits.keys()
            for blockers in blockers_by_thread[thread_id]
            for blocker in blockers
        }
        can_yield = {
            thread_id
            for thread_id in (stuck & in_the_way & {collection.thread_id for collection in queue}) - self._cannot_yield
            if thread_id in self._waited_on_by(thread_id, stuck, blockers_by_thread)
        }
        if not can_yield:
            return None
        yielding = self._select_yielding_thread(can_yield)
        return _Deadlock(yielding, self._hand_overs(yielding, stuck, blockers_by_thread))

    @staticmethod
    def _waited_on_by(
        thread_id: str, stuck: Set[str], blockers_by_thread: Dict[str, List[Set[str]]],
    ) -> Set[str]:
        """Every stuck thread this one waits for, directly or through others. It holds the thread
        itself when the thread is on a cycle."""
        reached: Set[str] = set()
        frontier = [thread_id]
        while frontier:
            found = {
                blocker
                for waiter in frontier
                for blockers in blockers_by_thread[waiter]
                for blocker in blockers & stuck
            } - reached
            reached |= found
            frontier = sorted(found)
        return reached

    def _hand_overs(
        self, yielding: str, stuck: Set[str], blockers_by_thread: Dict[str, List[Set[str]]],
    ) -> Dict[str, str]:
        """Each location the yielding thread blocks, mapped to the placement waiter that gets it.

        The waiter must be one the yielding thread itself waits for, directly or through others.
        Any other waiter would take the freed location, need what the cycle still holds, and
        put the same deadlock back.
        """
        hand_overs: Dict[str, str] = {}
        seen = {yielding}
        frontier = [yielding]
        while frontier:
            reached = sorted({
                blocker
                for thread_id in frontier
                for blockers in blockers_by_thread[thread_id]
                for blocker in blockers & stuck
            } - seen)
            for thread_id in reached:
                request = self._location_waits.get(thread_id)
                if request is not None and yielding in blockers_by_thread[thread_id][0]:
                    hand_overs.setdefault(request.requested_location.position_id, thread_id)
            seen.update(reached)
            frontier = reached
        return hand_overs

    def _apply(self, deadlock: _Deadlock) -> None:
        if self._hand_over is None:
            return
        for position_id, thread_id in deadlock.hand_overs.items():
            self._hand_over(position_id, thread_id)

    def find_unresolvable_blocker(
        self,
        collection: IReservationCollection,
    ) -> UnresolvableDeadlockContext | None:
        """Per-collection check for an out-of-queue immovable blocker.

        The knot detector (`find_yielding_thread`) computes its stuck set
        over queued requests, placement waits and co-labware waits. A thread
        parked on `orca.join` is none of those, so its plate contributes no
        blocker and a knot through it is never seen.

        This method covers that gap by consulting the full
        thread registry for the blocker's owning thread, then declaring
        an unresolvable deadlock only when that thread's template has
        `immovable=True`. The narrow gate keeps normal slow waits free of
        false-positive declarations.

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
        deadlock = self._find_deadlock(queue)
        if deadlock is None:
            return
        self._apply(deadlock)

        for collection in queue:
            if collection.thread_id == deadlock.yielding_thread_id:
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
            if collection.granted.is_set():
                self._cannot_yield.discard(collection.thread_id)
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
        deadlock = self._find_deadlock(carry_list) if carry_list else None
        if deadlock is None:
            return
        self._apply(deadlock)
        yielding_thread_id = deadlock.yielding_thread_id

        orca_logger.info(
            f"Cross-tick deadlock detected: flagging thread {yielding_thread_id}"
        )
        self._deadlocked_threads.add(yielding_thread_id)
        self._rejected_carry.pop(yielding_thread_id, None)

    def _blockers_by_thread(self, queue: Sequence[IReservationCollection]) -> Dict[str, List[Set[str]]]:
        """Every waiting thread's candidates, each as the set of threads blocking it.

        A queued request has one candidate per location it would take. A placement wait has
        one candidate, its location. A co-labware wait has one candidate blocked by the thread
        of every plate still missing: it needs all of them, so one stuck plate makes it stuck.
        """
        outside_the_queue = self._location_waits.keys() | self._co_labware_waits.keys()
        queued = [collection for collection in queue if collection.thread_id not in outside_the_queue]
        labware_to_thread = self._labware_to_thread(
            [collection.thread_id for collection in queued] + sorted(outside_the_queue)
        )
        blockers_by_thread: Dict[str, List[Set[str]]] = {}
        for collection in queued:
            thread_id = collection.thread_id
            blockers_by_thread[thread_id] = [
                self._blocker_thread_ids(reservation, labware_to_thread, thread_id)
                for reservation in collection.get_reservations()
            ]
        for thread_id, request in self._location_waits.items():
            blockers_by_thread[thread_id] = [
                self._blocker_thread_ids(request, labware_to_thread, thread_id)
            ]
        for thread_id, missing_labware_ids in self._co_labware_waits.items():
            bringing = {
                labware_to_thread[labware_id]
                for labware_id in missing_labware_ids()
                if labware_id in labware_to_thread
            }
            blockers_by_thread[thread_id] = [bringing - {thread_id}]
        return blockers_by_thread

    @staticmethod
    def _stuck_among(blockers_by_thread: Dict[str, List[Set[str]]]) -> Set[str]:
        """The threads in a deadlock, by greatest fixpoint.

        A thread proceeds if ANY of its candidates frees, and a candidate frees once ALL its
        blockers release. A blocker that is not itself stuck releases on its own. So start with
        every waiting thread and keep removing any thread with a candidate whose blockers are all
        outside the set (a free candidate counts). What is left is stuck: every candidate of every
        member is blocked by another member.
        """
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
        NOT ranked, because the gate does not rank them either. A mutex holder
        whose action takes the requester's plate is no blocker: the gate lets
        that plate onto the device's sites.
        """
        blockers: Set[str] = set()
        location = reservation.requested_location
        mutex_key = location.owner_mutex_id
        if mutex_key is not None:
            holder = self._reservation_at(mutex_key)
            if holder is not None and holder.thread_id is not None and not self._lets_in(holder, reservation):
                blockers.add(holder.thread_id)
        blocker = self._blocking_thread(
            reservation, self._reservation_at(location.position_id),
        )
        if blocker is not None:
            blockers.add(blocker)
        promised = self._promised_to(location.position_id) if self._promised_to is not None else None
        if promised is not None:
            blockers.add(promised)

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
    def _lets_in(mutex_holder: LocationReservation, request: LocationReservation) -> bool:
        """True when the device's holder is running an action the requester's plate is part of."""
        membership = mutex_holder.membership
        return request.labware is not None and membership is not None and membership(request.labware.id)

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

    def _labware_to_thread(self, thread_ids: Sequence[str]) -> Dict[str, str]:
        labware_to_thread: Dict[str, str] = {}
        for thread_id in thread_ids:
            try:
                thread = self._thread_registry.get_thread(thread_id)
            except KeyError:
                thread = None
            if thread is None:
                orca_logger.warning(
                    f"Thread {thread_id} not found in registry during deadlock detection"
                )
                continue
            if thread.labware is None:
                orca_logger.warning(
                    f"Thread {thread_id} has no labware during deadlock detection"
                )
                continue
            labware_id = thread.labware.id
            if labware_id in labware_to_thread:
                orca_logger.warning(
                    f"Duplicate labware ID {labware_id} for threads "
                    f"{labware_to_thread[labware_id]} and {thread_id}"
                )
            labware_to_thread[labware_id] = thread_id
        return labware_to_thread
