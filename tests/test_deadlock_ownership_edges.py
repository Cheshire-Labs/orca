"""Deadlock blockers come from reservation ownership, not just resident labware.

Under the flat model a device's reservation key is an off-graph mutex Location
that plates never land on, and an ownership rejection (``can_reserve``'s Gate A)
fires while the requested SITE is still empty -- the holder has not placed yet.
An earlier detector derived blockers purely from labware sitting at the requested
location, so those rejections produced no blocker at all and the deadlock was
invisible; the threads then spin forever (``action_reservation_timeout`` is None).

These pin ``_blocker_thread_ids`` -- the per-candidate blocker derivation the knot
detector reads. The blocker lookup mirrors ``can_reserve``'s decision: the owner
mutex holder, a reservation on the position itself, and any in-queue resident
plate, UNIONED (a candidate frees only when every blocker clears), with the
requesting thread excluded (a self-held resource is a re-entrant grant, not a
block). The fixpoint verdict those blockers feed is pinned in test_deadlock_knot.
"""
import asyncio
from typing import Dict, List
from unittest.mock import Mock

from orca.state.identity import LabwareRef

from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.deck_site import DeckSite
from orca.resource_models.location import Location
from orca.system.reservation_manager.deadlock_manager import (
    DeadlockStarvationRegistry,
    ThreadDeadlockDetector,
)
from orca.system.reservation_manager.errors import UnresolvableDeadlockContext
from orca.system.reservation_manager.interfaces import IReservationCollection
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance


class _Owner:
    def __init__(self, name: str) -> None:
        self.name = name


class _ThreadRegistry(IThreadRegistry):
    """Threads carry no labware, so the labware fallback can never fire unless a
    test registers a holder -- every blocker under test then comes from
    reservation ownership."""

    def __init__(self, thread_ids: List[str]) -> None:
        self._threads: Dict[str, LabwareThreadInstance] = {}
        for thread_id in thread_ids:
            thread = Mock()
            thread.labware = None
            self._threads[thread_id] = thread

    def add_holder(self, thread_id: str, labware: Mock) -> None:
        """Register a thread that owns a plate, so the labware fallback can see it."""
        thread = Mock()
        thread.labware = labware
        self._threads[thread_id] = thread

    @property
    def threads(self) -> List[LabwareThreadInstance]:
        return list(self._threads.values())

    def get_thread(self, id: str) -> LabwareThreadInstance:
        return self._threads[id]

    def get_thread_by_labware(self, labware_id: str) -> LabwareThreadInstance:
        raise NotImplementedError

    def add_thread(self, labware_thread: LabwareThreadInstance) -> None:
        raise NotImplementedError


class _Collection(IReservationCollection):
    """Only ``thread_id`` and ``get_reservations`` are read while deriving
    blockers; the rest of the collection protocol is inert here."""

    def __init__(self, thread_id: str, reservations: List[LocationReservation]) -> None:
        self._thread_id = thread_id
        self._reservations = reservations
        self._events = {name: asyncio.Event() for name in
                        ("processed", "granted", "rejected", "deadlocked",
                         "unresolvable_deadlock")}

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def get_reservations(self) -> List[LocationReservation]:
        return self._reservations

    @property
    def processed(self) -> asyncio.Event:
        return self._events["processed"]

    @property
    def granted(self) -> asyncio.Event:
        return self._events["granted"]

    @property
    def rejected(self) -> asyncio.Event:
        return self._events["rejected"]

    @property
    def deadlocked(self) -> asyncio.Event:
        return self._events["deadlocked"]

    @property
    def unresolvable_deadlock(self) -> asyncio.Event:
        return self._events["unresolvable_deadlock"]

    @property
    def unresolvable_deadlock_context(self) -> UnresolvableDeadlockContext | None:
        return None

    def set_unresolvable_deadlock(self, context: UnresolvableDeadlockContext) -> None:
        raise NotImplementedError

    def resolve_final_reservation(self) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


def _site(node_id: str, mutex_id: str) -> DeckSiteLocation:
    return DeckSiteLocation(
        node_id,
        owner=_Owner(mutex_id),
        resource=DeckSite(node_id),
        mutex_position_id=mutex_id,
    )


def _detector(
    held: Dict[str, str | None], thread_ids: List[str]
) -> tuple[ThreadDeadlockDetector, _ThreadRegistry]:
    """``held`` maps position_id -> holding thread_id (None = system hold)."""
    entries: Dict[str, LocationReservation] = {}
    for position_id, thread_id in held.items():
        reservation = LocationReservation(Location(position_id))
        reservation.thread_id = thread_id
        entries[position_id] = reservation
    registry = _ThreadRegistry(thread_ids)
    detector = ThreadDeadlockDetector(
        registry, DeadlockStarvationRegistry(), reservation_at=entries.get
    )
    return detector, registry


def test_ownership_rejection_on_an_empty_site_names_the_mutex_holder() -> None:
    """The regression: Gate A rejects because the OWNER MUTEX is held while the
    site itself is empty, so nothing is resident to derive a blocker from -- the
    mutex holder must be named."""
    detector, _registry = _detector({"mlstar_1": "thread-b"}, ["thread-a", "thread-b"])
    request = LocationReservation(_site("mlstar_1/carrier-7-0", "mlstar_1"))

    assert detector._blocker_thread_ids(request, {}, "thread-a") == {"thread-b"}


def test_owner_mutex_and_site_reservation_are_both_blockers() -> None:
    """``can_reserve`` checks ownership before the position's own reservation, but
    for deadlock the candidate frees only when BOTH clear, so both holders are
    blockers. (The old single-blocker model named only the mutex holder; asserting
    the site holder was NOT a blocker was the modeling error -- a candidate held by
    an out-of-set thread AND an in-set thread must read blocked.)"""
    detector, _registry = _detector(
        {"mlstar_1": "thread-b", "mlstar_1/carrier-7-0": "thread-c"},
        ["thread-a", "thread-b", "thread-c"],
    )
    request = LocationReservation(_site("mlstar_1/carrier-7-0", "mlstar_1"))

    assert detector._blocker_thread_ids(request, {}, "thread-a") == {"thread-b", "thread-c"}


def test_a_system_hold_contributes_no_blocker() -> None:
    """A ``thread_id=None`` hold (a converted torn-placed
    reservation or a manual hold) is stall-detector territory, not a wait-for
    blocker -- it has no owning thread to blame."""
    detector, _registry = _detector({"mlstar_1": None}, ["thread-a"])
    request = LocationReservation(_site("mlstar_1/carrier-7-0", "mlstar_1"))

    assert detector._blocker_thread_ids(request, {}, "thread-a") == set()


def test_a_self_held_mutex_is_excluded_but_a_foreign_site_holder_is_not() -> None:
    """Holding a device's mutex does not stop its sites from blocking you.

    The holdover keeps A's mutex across a same-device next action, so Gate A
    passes re-entrantly and ``can_reserve`` rejects further down. The self-held
    mutex must be excluded (re-entrant grant) while the foreign site holder is
    still a blocker -- the requester must not mask a real foreign block.
    """
    detector, _registry = _detector(
        {"mlstar_1": "thread-a", "mlstar_1/carrier-7-0": "thread-b"},
        ["thread-a", "thread-b"],
    )
    request = LocationReservation(_site("mlstar_1/carrier-7-0", "mlstar_1"))

    assert detector._blocker_thread_ids(request, {}, "thread-a") == {"thread-b"}


def test_a_self_held_mutex_still_falls_through_to_a_resident_plate_blocker() -> None:
    """Same shape, but the site is blocked by a RESIDENT plate rather than a
    reservation: the occupancy fallback must still be reached, and the in-queue
    plate's owner named -- the labware map is built from the queue's collections."""
    site = _site("mlstar_1/carrier-7-0", "mlstar_1")
    blocker = Mock()
    blocker.id = "plate-b"
    blocker.ref = LabwareRef(id="plate-b", name="plate-b")
    site.resource.initialize_labware(blocker)

    detector, registry = _detector({"mlstar_1": "thread-a"}, ["thread-a"])
    registry.add_holder("thread-b", blocker)
    queue = [
        _Collection("thread-a", [LocationReservation(site)]),
        _Collection("thread-b", [LocationReservation(Location("pad_1"))]),
    ]
    labwares_in_queue = detector._get_labware_to_thread_map(queue)

    assert detector._blocker_thread_ids(
        LocationReservation(site), labwares_in_queue, "thread-a"
    ) == {"thread-b"}


def test_a_resource_held_only_by_the_requester_is_no_blocker() -> None:
    """A thread can hold a location out-of-band (``try_reserve_location``) while a
    stale carry still requests it, so the lookup can see the requester itself --
    which must be excluded, or the requester would be judged blocked by its own
    hold and parked while blocking nobody."""
    detector, _registry = _detector({"pad_1": "thread-a"}, ["thread-a"])
    request = LocationReservation(Location("pad_1"))

    assert detector._blocker_thread_ids(request, {}, "thread-a") == set()
