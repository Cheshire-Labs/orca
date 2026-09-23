from abc import ABC, abstractmethod
import asyncio
from typing import Dict, Iterable, List

from orca.resource_models.location import Location
from orca.system.reservation_manager.errors import UnresolvableDeadlockContext
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
    ReservationPriority,
)


class IAvailabilityManager(ABC):
    def is_location_available(self, location: Location) -> bool:
        raise NotImplementedError

    async def await_available(self, location: Location) -> None:
        raise NotImplementedError


class IReservationManager(ABC):
    @abstractmethod
    def can_reserve(
        self,
        position_id: str,
        thread_id: str | None = None,
        requesting_labware_id: str | None = None,
        requesting_priority: ReservationPriority = ReservationPriority.ORDINARY,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def release_reservation(self, position_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def release_reservation_by_id(self, reservation_id: str) -> str | None:
        """Release the reservation with this id and return its location name.

        Returns None if no active reservation has that id. Callers that need
        strictness should treat None as "not found" and raise upstream.
        """
        raise NotImplementedError


class IReservationCollection(ABC):
    @property
    @abstractmethod
    def thread_id(self) -> str:
        """Returns the ID of the thread that owns this reservation collection."""
        raise NotImplementedError

    @abstractmethod
    def get_reservations(self) -> List[LocationReservation]:
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

    @property
    @abstractmethod
    def unresolvable_deadlock(self) -> asyncio.Event:
        """An event set when this collection is blocked by an unresolvable deadlock.

        Round 1: signaled by `ThreadDeadlockDetector.find_unresolvable_blocker`
        when the blocker labware is owned by a thread template marked
        `immovable=True`. The resolver raises `UnresolvableDeadlockError`
        instead of retrying. Reusable for future deadlock variants without
        new events.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def unresolvable_deadlock_context(self) -> UnresolvableDeadlockContext | None:
        """Diagnostic context attached when `unresolvable_deadlock` is set."""
        raise NotImplementedError

    @abstractmethod
    def set_unresolvable_deadlock(self, context: UnresolvableDeadlockContext) -> None:
        """Atomically attach context AND signal the event.

        The detector calls this from inside `_on_tick`'s per-collection loop,
        BEFORE `processed.set()`, so the resolver observes the event on the
        SAME collection object it submitted (not a fresh one constructed on
        the next retry iteration).
        """
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> None:
        """Clears the reservation collection, resetting all events and states."""
        raise NotImplementedError

    @property
    def resets_starvation_on_grant(self) -> bool:
        """False for per-hop move collections: a granted HOP is not progress.

        A parked thread's return hop to the pad it just vacated is granted
        trivially; resetting on it re-crowns the same victim every lap (the
        N=6 boomerang livelock). Moves reset at ARRIVAL instead -- when the
        resolver reaches a true target (``MoveHandler.resolve_move_action``,
        the same site that clears the pad cooldown). Action acquisitions keep
        the on-grant reset: a granted device IS the progress being waited on.
        """
        return True


class IThreadReservationCoordinator(ABC):
    @abstractmethod
    async def submit_reservation_request(self, thread_id: str, request: IReservationCollection) -> None:
        raise NotImplementedError

    @abstractmethod
    async def try_reserve_location(
        self, thread_id: str, position_id: str, request: LocationReservation
    ) -> bool:
        """Single-shot attempt to reserve a location, bypassing the move queue
        and deadlock detector. True iff granted.

        The bypass keeps the request out of the wait-for graph, so a caller must
        guarantee its reservation cannot sit in an undetected deadlock cycle:
        either the target is non-routable (a deck child site never becomes a
        park target) or the caller holds no other reservation (a spawn placing a
        fresh plate is a leaf waiter). Used for spawn placement
        (``MoveHandler.acquire_placement_reservation``).
        """
        raise NotImplementedError

    @abstractmethod
    def release_snapshot(self, position_ids: Iterable[str]) -> Dict[str, int]:
        """Snapshot current release counts for a waiter's candidate locations.

        Passed to ``wait_for_location_release`` so a release of one of these
        locations between the waiter's attempt and its wait is not slept through.
        """
        raise NotImplementedError

    @abstractmethod
    async def wait_for_location_release(
        self, snapshot: Dict[str, int], timeout: float
    ) -> None:
        """Return when a location in ``snapshot`` is released past its snapshotted
        count, or after ``timeout`` seconds, whichever comes first.

        Lets a rejected waiter wake the instant one of ITS OWN contended
        locations frees; an unrelated release only nudges it back to sleep (no
        re-submission), which keeps the tick uncongested. ``timeout`` is the
        load-bearing safety cap that keeps re-submissions flowing so the
        cross-tick deadlock detector still runs when a genuine deadlock yields no
        releases.
        """
        raise NotImplementedError

    @abstractmethod
    async def start_tick_loop(self) -> None:
        """Starts the tick loop for the reservation coordinator."""
        raise NotImplementedError

    @abstractmethod
    def stop_tick_loop(self) -> None:
        """Stop the tick loop. Called on workflow completion or shutdown."""
        raise NotImplementedError

    @abstractmethod
    def get_active_reservations(self) -> list[tuple[str, str, str | None]]:
        """Returns list of (position_id, reservation_id, thread_id) for all active reservations.

        ``thread_id`` is None for reservations not owned by any thread
        (system-held / manual holds).
        """
        raise NotImplementedError

    @abstractmethod
    def get_reservation_at(self, position_id: str) -> LocationReservation | None:
        """The reservation holding ``position_id``, or None.

        The object rather than its id, so a caller that has to explain a
        refusal can read what is coming and under what tier.
        """
        raise NotImplementedError

    @abstractmethod
    def get_reserved_position_ids(self, exclude_thread_id: str | None = None) -> set[str]:
        """Return position_ids with active reservations, optionally excluding one thread."""
        raise NotImplementedError

    @abstractmethod
    def cancel_reservation_by_id(self, reservation_id: str) -> tuple[str, str | None]:
        """Cancel an active reservation and return (position_id, thread_id).

        ``thread_id`` is ``None`` when the cancelled reservation had no owning
        thread (system-held reservations, manual holds without an executing
        thread). Callers that care about execution-scoping should validate
        the returned thread_id belongs to the expected execution before
        calling this; the coordinator itself does not know about executions.

        Raises KeyError if no active reservation has this id.
        """
        raise NotImplementedError

    @abstractmethod
    def mark_threads_dead(self, thread_ids: set[str]) -> None:
        """Record threads whose queued requests must never be granted.

        The tick loop is system-wide and runs through an execution abort, so a
        request a cancelled thread already queued must not be granted after the
        thread is gone. Synchronous so the mark is visible to the next tick.
        """
        raise NotImplementedError

    @abstractmethod
    def release_reservations_for_threads(self, thread_ids: set[str]) -> list[str]:
        """Release every active reservation held by any of the given threads.

        Returns the released position ids. The abort sweep for reservations a
        cancelled thread never bound to a releasable field.
        """
        raise NotImplementedError

    @abstractmethod
    def forget_threads(self, thread_ids: set[str]) -> None:
        """Drop stale per-thread deadlock bookkeeping for gone threads."""
        raise NotImplementedError
