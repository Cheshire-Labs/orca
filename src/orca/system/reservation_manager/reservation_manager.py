
import asyncio
import logging
from typing import Callable, Dict, Iterable, List
from orca.resource_models.location import ILabwareLocationObserver, Location
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    ReservationPriority,
    outranks,
)
from orca.system.reservation_manager.deadlock_manager import DeadlockStarvationRegistry, ThreadDeadlockDetector
from orca.system.reservation_manager.interfaces import IAvailabilityManager, IReservationCollection, IReservationManager, IThreadReservationCoordinator
from orca.system.system_map import ILocationRegistry
from orca.system.thread_registry_interface import IThreadRegistry

orca_logger = logging.getLogger("orca")

class LocationReservationManager(IReservationManager, IAvailabilityManager, ILabwareLocationObserver):
    def __init__(
        self,
        location_reg: ILocationRegistry,
        exclusion_siblings_of: Callable[[str], List[Location]] | None = None,
    ) -> None:
        self._location_reg = location_reg
        # Single-carriage groups (translators): a position's siblings are
        # the other taught stations of the same physical pad.
        self._exclusion_siblings_of = exclusion_siblings_of
        self._reservations: Dict[str, LocationReservation] = {}
        self._lock = asyncio.Lock()  # Protects reservation operations from race conditions
        self._on_release_callback: Callable[[str], None] | None = None

    @property
    def reservations(self) -> Dict[str, LocationReservation]:
        """Returns the current reservations."""
        return self._reservations

    def get_reservation_at(self, position_id: str) -> LocationReservation | None:
        """Returns the reservation for the given position_id, if it exists."""
        return self._reservations.get(position_id, None)

    async def attempt_reservation(self, position_id: str, request: LocationReservation,
                                   thread_id: str | None = None) -> None:
        """
        Attempts to reserve a location for the given request.

        Uses a lock to prevent TOCTOU (Time-Of-Check Time-Of-Use) race conditions
        where multiple threads could simultaneously check availability and both
        attempt to reserve the same location.

        If thread_id is provided and the location is already reserved by the same
        thread, the reservation is re-granted (re-entrant). This supports
        same-location consecutive actions without release-and-reacquire.

        Round 5 S1-B: when ``request.labware`` is set the reservation
        becomes thread-aware about labware identity. ``can_reserve`` grants
        if the location's current labware IS the requester's own labware
        (entry thread targeting own start_location -- the user's session
        repro). Cross-thread occupancy still rejects so
        ``ThreadDeadlockDetector`` continues to see the cycle signal.
        """
        requesting_labware_id = request.labware.id if request.labware is not None else None
        async with self._lock:
            self._maybe_takeover_drained(position_id, requesting_labware_id)
            mutex_key = self._location_reg.get_location(position_id).owner_mutex_id
            if mutex_key is not None:
                self._maybe_takeover_drained(mutex_key, requesting_labware_id)
            if self.can_reserve(
                position_id, thread_id,
                requesting_labware_id=requesting_labware_id,
                requesting_priority=request.priority,
            ):
                self._reserve(position_id, request, thread_id)
                request.granted.set()
            else:
                request.rejected.set()
            request.processed.set()

    def _reserve(self, position_id: str, request: LocationReservation,
                 thread_id: str | None = None) -> None:
        old = self._reservations.get(position_id)
        # A re-entrant re-grant of the SAME object is not a displacement; marking
        # it would neuter the live holder's own release.
        if old is not None and old is not request:
            old.set_reservation_release_callback(lambda: None)
            old.mark_displaced()
            # Same-thread displacement is normal hold consumption (a crossing
            # leg taking over its insured landing); only cross-thread is news.
            log = orca_logger.debug if old.thread_id == thread_id else orca_logger.info
            log(
                f"Displacing reservation {old.id} at {position_id} "
                f"(thread {old.thread_id} -> {thread_id})"
            )
        self._reservations[position_id] = request
        request.thread_id = thread_id
        request.set_location(self._location_reg.get_location(position_id))
        request.set_reservation_release_callback(lambda: self.release_reservation(position_id))
        labware_name = request.labware.name if request.labware else "unknown"
        orca_logger.info(f"Thread {labware_name} - Reservation {request.id} granted for {position_id}")

    def can_reserve(
        self,
        position_id: str,
        thread_id: str | None = None,
        requesting_labware_id: str | None = None,
        requesting_priority: ReservationPriority = ReservationPriority.ORDINARY,
    ) -> bool:
        """Reservation gate -- holder exclusivity + thread-aware occupancy.

        Three outcomes when ``existing is None`` (no current reservation):

        1. Location empty -> grant. Standard case.
        2. Location holds the requester's OWN labware -> grant. This is
           the user's Round 5 session repro: entry thread with its own
           labware at its own start_location. Pre-fix this returned False
           because the layer conflated reservation-holder exclusivity
           with labware-slot availability; the thread sat in
           ``RESOLVING_ACTION_LOCATION`` forever because nothing else
           would ever release "the reservation" (there was no reservation
           to release). Identifies "own labware" by labware id match.
        3. Location holds another thread's labware -> reject. Cross-thread
           occupancy still trips the rejection path that
           ``ThreadDeadlockDetector`` uses to find cycles. This is the
           genuine wait-for-other-thread case; rejecting here preserves
           the cycle-detection signal pre-fix relied on.

        ``existing is not None`` (someone holds a reservation): grants
        re-entrantly for the matching ``thread_id`` (hold-over for
        same-device consecutive actions). Another thread's hold rejects
        unless the requester outranks it (``ReservationPriority.outranks``),
        and an outranking requester still has to clear the occupancy outcomes
        above: a plate standing on the spot beats every tier.

        Callers that did not previously pass labware identity still get
        the old "any-occupancy rejects" behavior because
        ``requesting_labware_id is None`` falls into outcome 3.

        """
        location = self._location_reg.get_location(position_id)
        #A device-owned site rejects a foreign
        # thread unless its labware is a live member of the holding action.
        mutex_key = location.owner_mutex_id
        if mutex_key is not None:
            holder = self._reservations.get(mutex_key)
            if holder is not None and (thread_id is None or holder.thread_id != thread_id):
                sanctioned = (
                    requesting_labware_id is not None
                    and holder.membership is not None
                    and holder.membership(requesting_labware_id)
                )
                if not sanctioned:
                    return False
        if self._sibling_blocked(position_id, thread_id, requesting_labware_id):
            return False
        existing = self._reservations.get(position_id)
        if existing is not None:
            if thread_id is not None and existing.thread_id == thread_id:
                return True
            if not outranks(requesting_priority, existing.priority):
                return False
        current_labware = location.labware
        if current_labware is None:
            return True
        if requesting_labware_id is not None and current_labware.id == requesting_labware_id:
            return True
        return False

    def _sibling_blocked(
        self,
        position_id: str,
        thread_id: str | None,
        requesting_labware_id: str | None,
    ) -> bool:
        """Single-occupancy carriage gate: a position rejects while a SIBLING
        position of the same physical carriage holds another thread's labware
        or reservation. The requester's own labware/reservation never blocks
        (that is the plate riding the carriage across).

        A sibling hold is NOT ranked, whatever the requester is. Granting over
        one would leave it standing: only the holder at the requested position
        is displaced, so an operator wait outranked on a sibling station would
        go on asking a person to fill a carriage a plate is landing on."""
        if self._exclusion_siblings_of is None:
            return False
        for sibling in self._exclusion_siblings_of(position_id):
            occupant = sibling.labware
            if occupant is not None and occupant.id != requesting_labware_id:
                return True
            holder = self._reservations.get(sibling.position_id)
            if holder is not None and (thread_id is None or holder.thread_id != thread_id):
                return True
        return False

    def is_location_available(self, location: Location) -> bool:
        """``IAvailabilityManager``: True iff the location holds no labware.

        Round 5 S1-B: was previously a stub even though
        ``LocationReservationManager`` declared ``IAvailabilityManager``
        as a parent. Delegates to ``Location.labware`` so callers can
        ask the manager without reaching into the location registry.
        """
        return location.labware is None

    async def await_available(self, location: Location) -> None:
        """``IAvailabilityManager``: park until the location's labware clears.

        Wraps ``Location.wait_until_available()`` which waits on the
        location's ``_availability_condition``. Used by the move layer
        via ``executing_labware_thread.py:1388-1389``; pulling it through
        ``IAvailabilityManager`` keeps the interface promise self-
        consistent so future callers don't need to reach into Location
        internals.
        """
        await location.wait_until_available()

    def _maybe_takeover_drained(
        self, position_id: str, requesting_labware_id: str | None
    ) -> None:
        """A holder whose action departed with
        occupants aboard is released the moment its drain predicate passes,
        evaluated here at acquisition time (never via a stored trigger). The
        requester's own labware never blocks its own takeover: granting the
        successor IS the handoff that lets a staying plate proceed."""
        holder = self._reservations.get(position_id)
        if holder is None:
            return
        check = holder.pending_drain_check
        if check is not None and check(requesting_labware_id):
            orca_logger.info(
                f"Drained holdover on {position_id} taken over "
                f"(reservation {holder.id})"
            )
            self.release_reservation(position_id)

    def release_reservation(self, position_id: str) -> None:
        if position_id in self._reservations.keys():
            reservation = self._reservations[position_id]
            orca_logger.info(f"Releasing reservation {reservation.id} for {position_id}")
            del self._reservations[position_id]
            # Neuter the released reservation's callback so a stale later call
            # (abort sweep, operator cancel) cannot delete a successor's entry.
            reservation.set_reservation_release_callback(lambda: None)
            reservation.mark_released()
            if self._on_release_callback is not None:
                self._on_release_callback(position_id)

    def release_reservation_by_id(self, reservation_id: str) -> str | None:
        target = next(
            (loc for loc, r in self._reservations.items() if r.id == reservation_id),
            None,
        )
        if target is None:
            return None
        self.release_reservation(target)
        return target

    def release_reservations_for_threads(self, thread_ids: set[str]) -> list[str]:
        """Release every reservation held by any of the given thread ids.

        The abort sweep: frees reservations a cancelled thread never bound to
        a releasable field (e.g. it was parked awaiting the grant), which its
        own terminal release could not reach. Synchronous and atomic under
        asyncio (no awaits), like release_reservation_by_id.
        """
        targets = [
            position_id for position_id, reservation in self._reservations.items()
            if reservation.thread_id in thread_ids
        ]
        for position_id in targets:
            self.release_reservation(position_id)
        return targets

    def get_all_active_reservations(self) -> list[tuple[str, str, str | None]]:
        """Returns list of (position_id, reservation_id, thread_id) for all active reservations.

        ``thread_id`` is None for reservations not owned by any executing
        thread (system-held or manual holds).
        """
        return [
            (loc_name, res.id, res.thread_id)
            for loc_name, res in self._reservations.items()
        ]

    def set_on_release_callback(self, callback: Callable[[str], None]) -> None:
        self._on_release_callback = callback



class ThreadReservationCoordinator(IThreadReservationCoordinator, IAvailabilityManager, ILabwareLocationObserver):
    def __init__(
        self,
        location_reg: ILocationRegistry,
        thread_registry: IThreadRegistry,
        exclusion_siblings_of: Callable[[str], List[Location]] | None = None,
    ) -> None:
        self._location_reg = location_reg
        self._reservation_manager: LocationReservationManager = LocationReservationManager(
            location_reg, exclusion_siblings_of=exclusion_siblings_of
        )
        self._queue: List[IReservationCollection] = []
        self._dead_thread_ids: set[str] = set()
        self._starvation_registry = DeadlockStarvationRegistry()
        self._deadlock_detector = ThreadDeadlockDetector(
            thread_registry,
            self._starvation_registry,
            reservation_at=self._reservation_manager.get_reservation_at,
            owned_sites_of=self._owned_sites_of_mutex,
            exclusion_siblings_of=exclusion_siblings_of,
        )

        self.ticker_started = False
        self._lock = asyncio.Lock()
        self._work_available = asyncio.Event()
        self._release_event = asyncio.Event()
        self._release_generations: Dict[str, int] = {}
        self._reservation_manager.set_on_release_callback(self._on_release)

    def _owned_sites_of_mutex(self, position_id: str) -> List[Location]:
        """Child sites of a device-mutex position, for detector occupancy edges."""
        return self._location_reg.sites_of(position_id)

    def _on_release(self, position_id: str) -> None:
        """Fired synchronously when the reservation on ``position_id`` is released.

        Wakes the tick loop (existing behavior) and nudges waiters parked in
        ``wait_for_location_release``. The PER-LOCATION count is the source of
        truth a waiter compares against; the shared event is only the wakeup
        nudge. Keeping the count per position_id is what makes an irrelevant
        release cheap: waiters for other locations wake, see their own candidate
        counts unchanged, and re-park WITHOUT re-submitting -- so the tick is not
        flooded and the deadlock detector does not false-fire (the failure mode
        of a global release signal).
        """
        self._release_generations[position_id] = self._release_generations.get(position_id, 0) + 1
        self._work_available.set()
        self._release_event.set()

    def release_snapshot(self, position_ids: Iterable[str]) -> Dict[str, int]:
        """Snapshot the current release counts for a waiter's candidate locations.

        Passed to ``wait_for_location_release`` so a release of one of THESE
        locations between the waiter's attempt and its wait advances the count
        and is not slept through.
        """
        return {pid: self._release_generations.get(pid, 0) for pid in position_ids}

    async def wait_for_location_release(
        self, snapshot: Dict[str, int], timeout: float
    ) -> None:
        """Return when a location in ``snapshot`` is released past its snapshotted
        count, or after ``timeout`` seconds, whichever comes first.

        THE TWO-GATE MODEL (the canonical statement; other docstrings refer
        here): a location grants only when BOTH gates pass -- the reservation
        gate (no other thread holds it) and the occupancy gate (no other
        labware physically resident; see ``can_reserve``). This wait fires on
        RESERVATION releases only. The occupancy gate clears on a PICK, which
        fires no release, so an occupancy-blocked waiter is woken only by the
        ``timeout`` cadence. (Extending the wait to the existing
        ``Location._availability_condition`` pick notifier is the tracked
        follow-up; until then, gate-2 waits poll exactly as they did before
        this change.)

        For the reservation gate this replaces a blind ``asyncio.sleep``: a
        waiter re-checks the instant one of ITS OWN contended locations frees,
        while an unrelated release only nudges it back to sleep (no
        re-submission). ``timeout`` is the load-bearing safety cap: it bounds a
        missed wakeup and keeps re-submissions flowing so the untouched
        cross-tick deadlock detector still runs when a genuine deadlock yields
        no releases.

        The per-location count compare is the linearization point; the shared
        event is only a nudge. A ``clear`` cannot steal an already-scheduled
        wakeup (``Event.set`` resolves every current waiter's future before any
        later ``clear``), and a waiter that calls ``wait`` after a ``clear`` is
        guarded by the recheck below. That clear->recheck->wait reasoning is
        exact on 3.12 (``wait_for`` awaits inline); on 3.10 ``wait_for`` routes
        through ``ensure_future`` so registration lands a loop-iteration later
        -- the ``timeout`` cap is what makes any missed wakeup benign there.
        """
        def candidate_freed() -> bool:
            return any(
                self._release_generations.get(pid, 0) != count
                for pid, count in snapshot.items()
            )

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            if candidate_freed():
                return
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            self._release_event.clear()
            if candidate_freed():
                return
            try:
                await asyncio.wait_for(self._release_event.wait(), remaining)
            except (asyncio.TimeoutError, TimeoutError):
                return

    @property
    def starvation_registry(self) -> DeadlockStarvationRegistry:
        """Get the starvation registry for thread prioritization."""
        return self._starvation_registry

    def get_active_reservations(self) -> list[tuple[str, str, str | None]]:
        """Returns list of (position_id, reservation_id, thread_id) for all active reservations.

        ``thread_id`` is None for reservations not owned by any thread.
        """
        return self._reservation_manager.get_all_active_reservations()

    def get_reservation_at(self, position_id: str) -> LocationReservation | None:
        return self._reservation_manager.get_reservation_at(position_id)

    def mark_threads_dead(self, thread_ids: set[str]) -> None:
        """Record threads whose queued requests must never be granted.

        The tick loop is system-wide and keeps running through an execution
        abort, so a request a cancelled thread already queued could be granted
        after the thread's terminal release ran -- a permanent orphan. ``_on_tick``
        refuses to grant for a dead thread. Synchronous (a plain set update) so
        the mark is visible to the next tick with no await gap to slip through.
        """
        self._dead_thread_ids.update(thread_ids)

    def release_reservations_for_threads(self, thread_ids: set[str]) -> list[str]:
        """Release every active reservation held by any of the given threads."""
        return self._reservation_manager.release_reservations_for_threads(thread_ids)

    def forget_threads(self, thread_ids: set[str]) -> None:
        """Drop stale per-thread deadlock bookkeeping for gone threads.

        ``_rejected_carry`` keeps a thread's last rejected collection (with live
        Location refs) for cross-tick cycle detection; left behind for an aborted
        thread it would feed a phantom cycle into a later execution.

        Deliberately does NOT clear ``_dead_thread_ids``: a tick woken by the
        sweep's release can still drain a dead thread's queued (unprocessed)
        request, and the dead mark is what makes that tick reject it instead of
        granting a fresh orphan.
        """
        self._deadlock_detector.forget_threads(thread_ids)

    def get_reserved_position_ids(self, exclude_thread_id: str | None = None) -> set[str]:
        """Return position_ids with active reservations, optionally excluding one thread."""
        reserved: set[str] = set()
        for position_id, reservation in self._reservation_manager.reservations.items():
            if exclude_thread_id is not None and reservation.thread_id == exclude_thread_id:
                continue
            reserved.add(position_id)
        return reserved

    def cancel_reservation_by_id(self, reservation_id: str) -> tuple[str, str | None]:
        """Cancel an active reservation, return (position_id, thread_id).

        ``thread_id`` is None when the reservation had no owning thread
        (system-held / manual). Releases via the underlying location manager
        (which fires the owner's release callback and wakes any threads
        waiting on that location). The coordinator does not validate which
        execution owns the thread -- that is a higher-layer concern. Raises
        KeyError if no active reservation has this id.
        """
        reservation = None
        for r in self._reservation_manager.reservations.values():
            if r.id == reservation_id:
                reservation = r
                break
        if reservation is None:
            raise KeyError(f"Reservation '{reservation_id}' not found")
        thread_id = reservation.thread_id
        position_id = self._reservation_manager.release_reservation_by_id(reservation_id)
        if position_id is None:
            # Lost to a concurrent release between the scan and the release.
            raise KeyError(f"Reservation '{reservation_id}' was released concurrently")
        orca_logger.info(
            f"Cancelled reservation {reservation_id} for {position_id} "
            f"(thread {thread_id})"
        )
        return position_id, thread_id

    async def start_tick_loop(self) -> None:
        """Event-driven tick loop: wakes on new requests or reservation releases."""
        if self.ticker_started:
            return
        self.ticker_started = True
        try:
            while True:
                await self._work_available.wait()
                self._work_available.clear()
                await self._on_tick()
        except asyncio.CancelledError:
            self.ticker_started = False
            raise

    def stop_tick_loop(self) -> None:
        """Cancel the tick loop task if running. Called on shutdown."""
        self.ticker_started = False
        # Wake the loop so it can observe cancellation
        self._work_available.set()

    async def _on_tick(self) -> None:
        """Process pending reservation requests and run deadlock detection."""

        async with self._lock:
            queue_snapshot = list(self._queue)
            self._queue.clear()

        queue_snapshot.sort(
            key=lambda c: self._deadlock_detector.get_starvation_score(c.thread_id),
            reverse=True,
        )

        for collection in queue_snapshot:
            if collection.thread_id in self._dead_thread_ids:
                collection.rejected.set()
                collection.processed.set()
                continue

            if self._deadlock_detector.is_flagged_for_deadlock(collection.thread_id):
                collection.deadlocked.set()
                collection.processed.set()
                continue

            for r in collection.get_reservations():
                await self._reservation_manager.attempt_reservation(
                    r.requested_location.name, r, thread_id=collection.thread_id)

            collection.resolve_final_reservation()
            # S3 Round 1: detect immovable-blocker rejections BEFORE the
            # resolver wakes on `processed` so it observes the event on
            # the SAME collection it submitted, not a fresh one
            # constructed on the next retry iteration. Single-collection
            # check; out-of-queue blockers reachable via the full thread
            # registry.
            #
            # Race-safety invariant: `resolve_final_reservation` already
            # calls `processed.set()` itself (util.py + move_handler.py).
            # The detector + `set_unresolvable_deadlock` call below run
            # synchronously, before any `await` that would yield to the
            # resolver's `await request.processed.wait()`. The trailing
            # `processed.set()` is a defensive no-op for the case where
            # a future `IReservationCollection` implementation does NOT
            # set `processed` inside `resolve_final_reservation`.
            # DO NOT add an `await` between resolve_final_reservation and
            # collection.processed.set() -- it reopens the race window.
            unresolvable_context = self._deadlock_detector.find_unresolvable_blocker(collection)
            if unresolvable_context is not None:
                collection.set_unresolvable_deadlock(unresolvable_context)
            collection.processed.set()

        # Exclude dead threads: a dead thread's rejected request must not re-enter
        # the deadlock carry (cross-tick detection has no liveness check).
        self._deadlock_detector.process_tick_results(
            [c for c in queue_snapshot if c.thread_id not in self._dead_thread_ids]
        )

    async def submit_reservation_request(self, thread_id: str, request: IReservationCollection) -> None:
        if self.ticker_started is False:
            orca_logger.warning("Reservation Coordinator Ticker not started.")
        async with self._lock:
            self._queue.append(request)
        self._work_available.set()

    async def try_reserve_location(
        self, thread_id: str, position_id: str, request: LocationReservation
    ) -> bool:
        """Single-shot attempt to reserve a location. Returns True iff granted.

        Bypasses the move queue on purpose. Two callers, and only one of them
        stays out of the wait-for graph: `_await_location_reservation` loops on
        this under `move_reservation_timeout` and does wait, while
        `hold_the_source` takes the slot its plate has not left yet and waits
        for nothing. The underlying manager lock still serializes both against
        tick-loop grants.
        """
        await self._reservation_manager.attempt_reservation(position_id, request, thread_id)
        return request.granted.is_set() and not request.is_displaced
