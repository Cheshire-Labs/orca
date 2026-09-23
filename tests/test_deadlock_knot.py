"""Deadlock detection is AND-OR knot detection, not digraph cycle finding.

A reservation collection is a pick-ONE (OR) wait: the thread proceeds if ANY
candidate frees. A thread set S is deadlocked iff EVERY thread in S has EVERY
candidate blocked by a thread inside S -- the greatest fixpoint reached by
repeatedly removing any thread with an escapable candidate (all blockers
outside S, including off-queue holders, which will release on their own).

Two failure modes this file pins against:
- FALSE POSITIVE: an edge-per-candidate digraph declares a cycle even when a
  free candidate exists; a false deadlock forcibly parks a live thread.
- FALSE NEGATIVE: a real knot can span MORE than one simple cycle, so any
  single-cycle (nx.find_cycle) validity filter rejects it and the threads spin
  forever.
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
    """Threads carry no labware unless registered via add_holder, so occupancy
    blockers appear only where a test explicitly plants them."""

    def __init__(self, thread_ids: List[str]) -> None:
        self._threads: Dict[str, LabwareThreadInstance] = {}
        for thread_id in thread_ids:
            thread = Mock()
            thread.labware = None
            self._threads[thread_id] = thread

    def add_holder(self, thread_id: str, labware: Mock) -> None:
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
) -> tuple[ThreadDeadlockDetector, _ThreadRegistry, DeadlockStarvationRegistry]:
    """``held`` maps position_id -> holding thread_id (None = system hold)."""
    entries: Dict[str, LocationReservation] = {}
    for position_id, thread_id in held.items():
        reservation = LocationReservation(Location(position_id))
        reservation.thread_id = thread_id
        entries[position_id] = reservation
    registry = _ThreadRegistry(thread_ids)
    starvation = DeadlockStarvationRegistry()
    detector = ThreadDeadlockDetector(registry, starvation, reservation_at=entries.get)
    return detector, registry, starvation


def test_or_wait_with_a_free_candidate_declares_no_deadlock() -> None:
    """Two threads each holding what the other also wants is not a deadlock
    while either still has a genuinely free candidate."""
    detector, _registry, _s = _detector(
        {"mlstar_1": "thread-b", "mlstar_2": "thread-a"},
        ["thread-a", "thread-b"],
    )
    queue = [
        _Collection("thread-a", [
            LocationReservation(_site("mlstar_1/carrier-7-0", "mlstar_1")),
            LocationReservation(Location("pad_1")),
        ]),
        _Collection("thread-b", [
            LocationReservation(_site("mlstar_2/carrier-7-0", "mlstar_2")),
            LocationReservation(Location("pad_2")),
        ]),
    ]

    assert detector.find_yielding_thread(queue) is None


def test_two_thread_mutex_knot_fires() -> None:
    detector, _registry, _s = _detector(
        {"mlstar_1": "thread-b", "mlstar_2": "thread-a"}, ["thread-a", "thread-b"]
    )
    queue = [
        _Collection("thread-a", [LocationReservation(_site("mlstar_1/carrier-7-0", "mlstar_1"))]),
        _Collection("thread-b", [LocationReservation(_site("mlstar_2/carrier-7-0", "mlstar_2"))]),
    ]

    assert detector.find_yielding_thread(queue) in {"thread-a", "thread-b"}


def test_union_of_cycles_knot_fires_and_includes_the_third_thread() -> None:
    """A:{P(B)}, B:{Q(A), R(C)}, C:{S(B)} is a TOTAL deadlock whose stuck set is
    a union of two simple cycles, not itself a simple cycle -- the shape any
    per-cycle validity filter rejects as false. thread-c's starvation is left
    lowest so its selection as yielder proves it is INSIDE the detected knot."""
    detector, _registry, starvation = _detector(
        {"held_by_b": "thread-b", "held_by_a": "thread-a", "held_by_c": "thread-c"},
        ["thread-a", "thread-b", "thread-c"],
    )
    starvation.increment_starvation_score("thread-a")
    starvation.increment_starvation_score("thread-b")
    queue = [
        _Collection("thread-a", [LocationReservation(Location("held_by_b"))]),
        _Collection("thread-b", [
            LocationReservation(Location("held_by_a")),
            LocationReservation(Location("held_by_c")),
        ]),
        _Collection("thread-c", [LocationReservation(Location("held_by_b"))]),
    ]

    assert detector.find_yielding_thread(queue) == "thread-c"


def test_off_queue_holder_makes_a_candidate_escapable() -> None:
    """A candidate held by a thread that is NOT waiting will free on its own;
    it must not sustain a knot. (Off-queue permanent blocks are the stall
    detector's / incident territory, per the documented boundary.)"""
    detector, _registry, _s = _detector(
        {"held_by_b": "thread-b", "held_by_offqueue": "thread-z",
         "held_by_a": "thread-a"},
        ["thread-a", "thread-b"],
    )
    # Without the off-queue rule this is a hard a<->b knot (a's second
    # candidate would read blocked); WITH it, a escapes via thread-z's hold.
    queue = [
        _Collection("thread-a", [
            LocationReservation(Location("held_by_b")),
            LocationReservation(Location("held_by_offqueue")),
        ]),
        _Collection("thread-b", [LocationReservation(Location("held_by_a"))]),
    ]

    assert detector.find_yielding_thread(queue) is None


def test_a_false_cycle_does_not_hide_a_disjoint_real_knot() -> None:
    """The old single-cycle detector could find the false cycle FIRST and
    report no deadlock at all; the fixpoint is whole-graph."""
    detector, _registry, _s = _detector(
        {
            "held_by_b": "thread-b", "held_by_a": "thread-a",
            "held_by_d": "thread-d", "held_by_c": "thread-c",
        },
        ["thread-a", "thread-b", "thread-c", "thread-d"],
    )
    queue = [
        # a<->b would be a cycle in the dense model, but a has a free candidate.
        _Collection("thread-a", [
            LocationReservation(Location("held_by_b")),
            LocationReservation(Location("free_pad")),
        ]),
        _Collection("thread-b", [LocationReservation(Location("held_by_a"))]),
        # c<->d is a genuine single-candidate knot.
        _Collection("thread-c", [LocationReservation(Location("held_by_d"))]),
        _Collection("thread-d", [LocationReservation(Location("held_by_c"))]),
    ]

    assert detector.find_yielding_thread(queue) in {"thread-c", "thread-d"}


def test_a_thread_whose_only_blocker_is_itself_is_not_deadlocked() -> None:
    """A self-held resource cannot block its own thread: without self-exclusion
    the seeded fixpoint would keep the requester stuck on its own hold and park
    a thread that is blocking nobody."""
    detector, _registry, _s = _detector({"pad_1": "thread-a"}, ["thread-a"])
    queue = [_Collection("thread-a", [LocationReservation(Location("pad_1"))])]

    assert detector.find_yielding_thread(queue) is None


def test_a_candidate_blocked_by_an_in_set_site_holder_is_not_escapable() -> None:
    """A candidate can have TWO blockers: an off-queue mutex holder AND an
    in-queue site holder. A mutex-first single-blocker lookup returns only the
    off-queue holder and mis-reads the candidate escapable (false negative);
    the full blocker set keeps the knot detectable."""
    detector, _registry, _s = _detector(
        {"mlstar_1": "thread-z", "mlstar_1/carrier-7-0": "thread-b",
         "held_by_a": "thread-a"},
        ["thread-a", "thread-b"],
    )
    queue = [
        _Collection("thread-a", [LocationReservation(_site("mlstar_1/carrier-7-0", "mlstar_1"))]),
        _Collection("thread-b", [LocationReservation(Location("held_by_a"))]),
    ]

    assert detector.find_yielding_thread(queue) in {"thread-a", "thread-b"}


def test_a_resident_plate_blocker_participates_in_a_knot() -> None:
    """The occupancy fallback (not a reservation): a candidate blocked by an
    in-queue thread's RESIDENT plate -- exposed via loaded_labware, e.g. a
    staging-bridge deck site -- must still count. Two threads each blocked by the
    other's resident plate form a knot."""
    site_a = _site("mlstar_1/carrier-7-0", "mlstar_1")
    site_b = _site("mlstar_2/carrier-7-0", "mlstar_2")
    plate_a, plate_b = Mock(), Mock()
    plate_a.id, plate_b.id = "plate-a", "plate-b"
    plate_a.ref = LabwareRef(id="plate-a", name="plate-a")
    plate_b.ref = LabwareRef(id="plate-b", name="plate-b")
    site_a.resource.initialize_labware(plate_b)
    site_b.resource.initialize_labware(plate_a)

    detector, registry, _s = _detector({}, [])
    registry.add_holder("thread-a", plate_a)
    registry.add_holder("thread-b", plate_b)
    queue = [
        _Collection("thread-a", [LocationReservation(site_a)]),
        _Collection("thread-b", [LocationReservation(site_b)]),
    ]

    assert detector.find_yielding_thread(queue) in {"thread-a", "thread-b"}


def test_a_none_hold_blocker_is_no_blocker() -> None:
    """A thread_id=None system hold (a 2E converted torn-placed reservation, or a
    manual hold) is stall-detector territory, not a wait-for blocker. A thread
    blocked only by a None hold is escapable, so no knot forms."""
    detector, _registry, _s = _detector(
        {"loc_a": None, "held_by_a": "thread-a"},
        ["thread-a", "thread-b"],
    )
    queue = [
        _Collection("thread-a", [LocationReservation(Location("loc_a"))]),
        _Collection("thread-b", [LocationReservation(Location("held_by_a"))]),
    ]

    assert detector.find_yielding_thread(queue) is None


def test_an_empty_collection_is_not_stuck() -> None:
    """'Every candidate blocked' is vacuously true of zero candidates; a thread
    requesting nothing must be treated as resolved, not deadlocked."""
    detector, _registry, _s = _detector(
        {"held_by_b": "thread-b", "held_by_a": "thread-a"}, ["thread-a", "thread-b"]
    )
    queue = [
        _Collection("thread-a", []),
        _Collection("thread-b", [LocationReservation(Location("held_by_a"))]),
    ]

    assert detector.find_yielding_thread(queue) is None
