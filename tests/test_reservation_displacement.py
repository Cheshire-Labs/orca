"""A displaced reservation must never be chosen as a collection's winner.

When a thread requests a location it already holds, ``_reserve`` transfers
ownership to the new reservation object and neuters the old one's release
callback. That transfer is deliberate -- it is how the hold-over hands a device
from one action to the next without the previous action's release killing the
live entry.

The hazard was a collection holding TWO reservations for one location (several
scored paths sharing a position). Both granted, the second displaced the first,
and winner selection could crown the displaced one and release the live one --
double-booking the location. Collection construction now collapses
same-position moves onto ONE reservation object, so sibling displacement is
structurally impossible; what remains reachable is displacement from OUTSIDE
the collection (hold-over handoff, drained takeover), and a displaced
reservation must still never be crowned.

These drive the real selection paths (``resolve_final_reservation`` on both
collection types), not a re-implementation of the filter.
"""
from typing import List
from unittest.mock import AsyncMock, Mock

import pytest

from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import (
    MoveActionCollectionReservationRequest,
    MoveHandler,
)
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
    ThreadReservationCoordinator,
)
from orca.system.system_map import ILocationRegistry
from orca.workflow_models.actions.util import LocationCollectionReservationRequest
from orca.workflow_models.actions.move_action import MoveAction


class _SingleLocationRegistry(ILocationRegistry):
    def __init__(self, location: Location) -> None:
        self._location = location

    @property
    def locations(self) -> List[Location]:
        return [self._location]

    def get_location(self, name: str) -> Location:
        return self._location

    def add_location(self, location: Location) -> None:
        raise NotImplementedError


def _manager() -> tuple[LocationReservationManager, Location]:
    location = Location("translator_2_start")
    return LocationReservationManager(_SingleLocationRegistry(location)), location


def _move(location: Location, labware: Mock) -> MoveAction:
    return MoveAction(labware, Location("source"), location, Mock())


@pytest.mark.asyncio
async def test_displacing_a_reservation_marks_the_old_one_displaced() -> None:
    manager, location = _manager()
    held = LocationReservation(location)
    displacing = LocationReservation(location)

    await manager.attempt_reservation("translator_2_start", held, thread_id="thread-a")
    await manager.attempt_reservation(
        "translator_2_start", displacing, thread_id="thread-a"
    )

    assert held.is_displaced, "the evicted reservation no longer owns the slot"
    assert not displacing.is_displaced, "the live holder must not be marked displaced"


@pytest.mark.asyncio
async def test_regranting_the_same_object_does_not_displace_it() -> None:
    """A re-entrant re-grant of the SAME object is not a displacement; marking
    it would neuter the live holder's own release."""
    manager, location = _manager()
    reservation = LocationReservation(location)

    for _ in range(2):
        await manager.attempt_reservation(
            "translator_2_start", reservation, thread_id="thread-a"
        )

    assert not reservation.is_displaced
    reservation.release_reservation()
    assert not manager.get_all_active_reservations(), (
        "the live holder's release must still free the slot"
    )


@pytest.mark.asyncio
async def test_same_target_moves_share_one_reservation_and_never_double_book() -> None:
    """The double-booking that wedged the SMC assay, pinned at its new choke
    point: collection construction collapses same-position moves onto ONE
    reservation object, so sibling requests can no longer displace each other
    and crowning can never release the slot the winner holds. (The old pin
    manufactured the displacement by attempting both objects BEFORE building
    the collection, an order the constructor now makes impossible.)"""
    manager, location = _manager()
    labware = Mock()
    labware.id = "plate-1"
    first, second = _move(location, labware), _move(location, labware)

    collection = MoveActionCollectionReservationRequest("thread-a", [first, second])
    assert second.reservation is first.reservation, (
        "same-target moves must ride one reservation object"
    )
    for reservation in collection.get_reservations():
        await manager.attempt_reservation(
            "translator_2_start", reservation, thread_id="thread-a"
        )
    collection.resolve_final_reservation()

    assert collection.granted.is_set()
    assert not collection.reserved_move_action.reservation.is_displaced, (
        "a displaced reservation must never be crowned"
    )
    assert manager.get_all_active_reservations(), (
        "crowning must leave the winner's slot held"
    )

    foreign = LocationReservation(location)
    await manager.attempt_reservation(
        "translator_2_start", foreign, thread_id="thread-b"
    )
    assert not foreign.granted.is_set(), (
        "a foreign thread was granted a location another thread still holds"
    )


@pytest.mark.asyncio
async def test_collection_whose_only_grant_is_displaced_rejects_and_retries() -> None:
    """A displaced-only collection must reject cleanly. ``clear()`` refuses to
    drop a granted reservation because that would orphan its lock, but a
    displaced one holds no lock -- and the retry has to reset it, or it is
    excluded from selection forever while the retry loop spins."""
    manager, location = _manager()
    labware = Mock()
    labware.id = "plate-1"
    only = _move(location, labware)

    await manager.attempt_reservation(
        "translator_2_start", only.reservation, thread_id="thread-a"
    )
    displacer = LocationReservation(location)
    await manager.attempt_reservation(
        "translator_2_start", displacer, thread_id="thread-a"
    )

    collection = MoveActionCollectionReservationRequest("thread-a", [only])
    collection.resolve_final_reservation()
    assert collection.rejected.is_set()

    collection.clear()
    assert not only.reservation.is_displaced, (
        "a retried reservation must compete again, not stay excluded forever"
    )


@pytest.mark.asyncio
async def test_action_location_collection_also_skips_displaced() -> None:
    """``LocationCollectionReservationRequest`` carries the same winner-selection
    shape for action locations. It is safe today only because a resource pool is
    unlikely to list one location twice, which is an accident rather than an
    invariant."""
    manager, location = _manager()
    first, second = LocationReservation(location), LocationReservation(location)

    await manager.attempt_reservation("translator_2_start", first, thread_id="thread-a")
    await manager.attempt_reservation("translator_2_start", second, thread_id="thread-a")

    system_map = Mock()
    system_map.get_distance.return_value = 1
    collection = LocationCollectionReservationRequest(
        "thread-a", [first, second], system_map, location
    )
    collection.resolve_final_reservation()

    assert collection.reserved_action_location is second, (
        "the live holder must win, not the displaced reservation"
    )
    assert manager.get_all_active_reservations(), (
        "releasing the displaced loser emptied the slot the winner holds"
    )


@pytest.mark.asyncio
async def test_try_reserve_location_reports_false_for_a_displaced_request() -> None:
    """``try_reserve_location`` must not report success for a request that reads
    granted but has been displaced. Its callers (deck-site residency, spawn
    placement) treat a True return as "I hold this slot"; handing that back for a
    reservation the manager has re-keyed to another object is the double-book in
    another guise."""
    location = Location("translator_2_start")
    coordinator = ThreadReservationCoordinator(_SingleLocationRegistry(location), Mock())
    held = LocationReservation(location)
    displacer = LocationReservation(location)

    await coordinator._reservation_manager.attempt_reservation(
        "translator_2_start", held, thread_id="thread-a"
    )
    await coordinator._reservation_manager.attempt_reservation(
        "translator_2_start", displacer, thread_id="thread-a"
    )
    assert held.granted.is_set() and held.is_displaced

    granted = await coordinator.try_reserve_location("thread-a", "translator_2_start", held)
    assert granted is False, (
        "a displaced request must report a failed reserve even though granted is set"
    )


@pytest.mark.asyncio
async def test_resolve_move_action_short_circuit_skips_a_displaced_move() -> None:
    """The resolve_move_action early-return grafts in the hold-over reservation and
    returns immediately if it is granted -- but a displaced graft must be skipped so
    the flow falls through to the collection resolver and the live holder wins."""
    coordinator = Mock()
    system_map = Mock()
    starvation = Mock()
    starvation.get_starvation_score.return_value = 0
    handler = MoveHandler(coordinator, system_map, starvation)

    current = Location("current")
    target = Location("target")
    system_map.get_all_shortest_any_paths.return_value = [["current", "target"]]
    handler._path_scorer = Mock()
    handler._path_scorer.score_paths.return_value = [Mock(path=["current", "target"])]

    displaced_move = Mock(spec=MoveAction)
    displaced_move.reservation = LocationReservation(target)
    displaced_move.reservation.granted.set()
    displaced_move.reservation.mark_displaced()
    displaced_move.onward_seat_reservations = []
    displaced_move.terminal_reservations = []
    displaced_move.owned_onward_reservations = []
    handler._get_potential_move_actions = Mock(return_value=[displaced_move])

    fallthrough = Mock(spec=MoveAction)
    fallthrough.target = target
    fallthrough.onward_seat_reservations = []
    fallthrough.terminal_reservations = []
    fallthrough.owned_onward_reservations = []
    fallthrough.shared_onward_reservations = []
    handler._resolve_reservation_from_move_action_collection = AsyncMock(
        return_value=fallthrough
    )

    labware = Mock()
    labware.id = "plate-1"
    result = await handler.resolve_move_action("thread-a", labware, current, [target])

    assert result is fallthrough, (
        "a displaced short-circuit move must be skipped, not early-returned"
    )
