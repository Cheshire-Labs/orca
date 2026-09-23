"""The blocked-episode state (pad cooldown + starvation debt) clears on
arrival at an interchangeable target, and only there.

``resolve_move_action`` takes a LIST of candidate sites: a
multi-site device offers every free owned site and the reservation grant picks
one. Two behaviours hang off that list:

- reaching any candidate clears the thread's cooldown, exactly as reaching a
  single named target does; otherwise ``handle_deadlock``'s avoid-set keeps
  excluding parking pads the thread left long ago;
- being diverted to a parking pad does NOT clear it, which is what stops the
  A->B->A oscillation the avoid-set exists to prevent.
"""
from typing import Dict, Iterable

import pytest

from orca.resource_models.devices import Device
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.deadlock_manager import DeadlockStarvationRegistry
from orca.system.reservation_manager.interfaces import (
    IReservationCollection,
    IThreadReservationCoordinator,
)
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.system_map import SystemMap
from tests.test_helpers import (
    create_simple_system_map,
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
)


_SHAKER_SITES = frozenset({"shaker_1/slot_a", "shaker_1/slot_b"})


class _StubCoordinator(IThreadReservationCoordinator):
    """Resolves the first submitted collection, then behaves as configured.

    ``deadlock_first`` makes the first submission come back deadlocked, which is
    how the resolver is pushed onto its parking-pad path. ``ungrantable`` names
    positions that never reserve: a diversion only reaches a pad while the real
    target stays blocked, since the flagged collection keeps asking for both.
    """

    def __init__(
        self,
        deadlock_first: bool = False,
        ungrantable: frozenset[str] = frozenset(),
    ) -> None:
        self._deadlock_first = deadlock_first
        self._ungrantable = ungrantable
        self.submissions = 0

    async def submit_reservation_request(
        self, thread_id: str, request: IReservationCollection
    ) -> None:
        self.submissions += 1
        if self._deadlock_first and self.submissions == 1:
            request.deadlocked.set()
            request.processed.set()
            return
        for reservation in request.get_reservations():
            if reservation.requested_location.position_id not in self._ungrantable:
                reservation.granted.set()
        request.resolve_final_reservation()

    async def try_reserve_location(
        self, thread_id: str, position_id: str, request: LocationReservation
    ) -> bool:
        raise NotImplementedError

    def release_snapshot(self, position_ids: Iterable[str]) -> Dict[str, int]:
        raise NotImplementedError

    async def wait_for_location_release(
        self, snapshot: Dict[str, int], timeout: float
    ) -> None:
        raise NotImplementedError

    async def start_tick_loop(self) -> None:
        raise NotImplementedError

    def stop_tick_loop(self) -> None:
        raise NotImplementedError

    def get_active_reservations(self) -> list[tuple[str, str, str | None]]:
        raise NotImplementedError

    def get_reservation_at(self, position_id: str) -> LocationReservation | None:
        raise NotImplementedError

    def get_reserved_position_ids(self, exclude_thread_id: str | None = None) -> set[str]:
        return set()

    def cancel_reservation_by_id(self, reservation_id: str) -> tuple[str, str | None]:
        raise NotImplementedError

    def mark_threads_dead(self, thread_ids: set[str]) -> None:
        raise NotImplementedError

    def forget_threads(self, thread_ids: set[str]) -> None:
        raise NotImplementedError

    def release_reservations_for_threads(self, thread_ids: set[str]) -> list[str]:
        raise NotImplementedError


async def _handler(
    coordinator: IThreadReservationCoordinator,
) -> tuple[MoveHandler, SystemMap, Device]:
    device = create_test_device("shaker_1", site_names=["slot_a", "slot_b"])
    transporter = create_test_transporter(
        "arm", ["pad_1", "pad_2", "shaker_1/slot_a", "shaker_1/slot_b"]
    )
    _registry, system_map = await create_simple_system_map(
        [transporter],
        {"shaker_1": device},
        {"pad_1": PlatePad("pad_1"), "pad_2": PlatePad("pad_2")},
    )
    handler = MoveHandler(coordinator, system_map, DeadlockStarvationRegistry())
    return handler, system_map, device


async def _two_hop_handler(
    coordinator: IThreadReservationCoordinator,
) -> tuple[MoveHandler, SystemMap]:
    """pad_1 -[arm_1]- pad_2 -[arm_2]- shaker sites: the shaker is two hops
    from pad_1, so the resolver's granted move targets the intermediate
    pad_2, never the requested target."""
    device = create_test_device("shaker_1", site_names=["slot_a", "slot_b"])
    arm_1 = create_test_transporter("arm_1", ["pad_1", "pad_2"])
    arm_2 = create_test_transporter(
        "arm_2", ["pad_2", "shaker_1/slot_a", "shaker_1/slot_b"]
    )
    _registry, system_map = await create_simple_system_map(
        [arm_1, arm_2],
        {"shaker_1": device},
        {"pad_1": PlatePad("pad_1"), "pad_2": PlatePad("pad_2")},
    )
    handler = MoveHandler(coordinator, system_map, DeadlockStarvationRegistry())
    return handler, system_map


@pytest.mark.asyncio
async def test_cooldown_clears_when_move_reaches_an_interchangeable_target() -> None:
    handler, system_map, _device = await _handler(_StubCoordinator())
    labware = await create_test_plate_template("plate_1").create_instance()
    thread_id = "thread-1"

    handler._recovery_strategy.record_visit(thread_id, "pad_1")
    assert handler._recovery_strategy.get_cooldown(thread_id)

    # Sourced the way production does, so a regression in sites_of that stopped
    # returning canonical graph nodes would surface here.
    await handler.resolve_move_action(
        thread_id,
        labware,
        system_map.get_location("pad_1"),
        system_map.sites_of("shaker_1"),
    )

    assert not handler._recovery_strategy.get_cooldown(thread_id), (
        "reaching one of the interchangeable targets must clear the cooldown; "
        "a Location-vs-list comparison silently never matches"
    )


@pytest.mark.asyncio
async def test_cooldown_survives_a_diversion_to_a_parking_pad() -> None:
    coordinator = _StubCoordinator(
        deadlock_first=True, ungrantable=_SHAKER_SITES,
    )
    handler, system_map, _device = await _handler(coordinator)
    labware = await create_test_plate_template("plate_1").create_instance()
    thread_id = "thread-1"

    handler._recovery_strategy.record_visit(thread_id, "pad_1")

    result = await handler.resolve_move_action(
        thread_id,
        labware,
        system_map.get_location("pad_1"),
        system_map.sites_of("shaker_1"),
    )

    assert isinstance(result.target.resource, PlatePad), (
        "expected the deadlock path to divert to a parking pad"
    )
    assert handler._recovery_strategy.get_cooldown(thread_id), (
        "a diversion to a parking pad must NOT clear the cooldown, or the "
        "avoid-set stops preventing A->B->A oscillation"
    )


@pytest.mark.asyncio
async def test_starvation_resets_on_arrival_at_target() -> None:
    """Reaching a requested target pays down the sacrifice debt, exactly
    where the pad cooldown clears (one 'episode escaped' definition)."""
    handler, system_map, _device = await _handler(_StubCoordinator())
    labware = await create_test_plate_template("plate_1").create_instance()
    thread_id = "thread-1"
    handler._starvation_registry.increment_starvation_score(thread_id)

    await handler.resolve_move_action(
        thread_id,
        labware,
        system_map.get_location("pad_1"),
        system_map.sites_of("shaker_1"),
    )

    assert handler._starvation_registry.get_starvation_score(thread_id) == 0


@pytest.mark.asyncio
async def test_starvation_survives_a_diversion_to_a_parking_pad() -> None:
    """A park grant is a yield: the flagged thread's score must ratchet, or
    the same victim is re-selected forever (the R1 livelock)."""
    coordinator = _StubCoordinator(
        deadlock_first=True, ungrantable=_SHAKER_SITES,
    )
    handler, system_map, _device = await _handler(coordinator)
    labware = await create_test_plate_template("plate_1").create_instance()
    thread_id = "thread-1"
    handler._starvation_registry.increment_starvation_score(thread_id)

    result = await handler.resolve_move_action(
        thread_id,
        labware,
        system_map.get_location("pad_1"),
        system_map.sites_of("shaker_1"),
    )

    assert isinstance(result.target.resource, PlatePad)
    assert handler._starvation_registry.get_starvation_score(thread_id) == 1, (
        "a diversion to a parking pad must not reset the starvation score"
    )


@pytest.mark.asyncio
async def test_starvation_survives_an_intermediate_hop_grant() -> None:
    """The boomerang pin: a granted hop short of the target is not progress.

    The parked victim's return hop to the spot it just vacated is granted
    trivially; resetting on it made the victim the unique minimum again
    every lap (the N=6 livelock). Only arrival resets.
    """
    handler, system_map = await _two_hop_handler(_StubCoordinator())
    labware = await create_test_plate_template("plate_1").create_instance()
    thread_id = "thread-1"
    handler._starvation_registry.increment_starvation_score(thread_id)

    result = await handler.resolve_move_action(
        thread_id,
        labware,
        system_map.get_location("pad_1"),
        system_map.sites_of("shaker_1"),
    )

    assert result.target.position_id == "pad_2", "granted move must be the first hop"
    assert handler._starvation_registry.get_starvation_score(thread_id) == 1, (
        "an intermediate hop grant must not reset the starvation score"
    )


@pytest.mark.asyncio
async def test_yield_vacate_arrival_does_not_escape_episode() -> None:
    """``escape_on_arrival=False`` (acquisition-yield vacate): reaching the
    pad is the yield itself, so neither the score nor the cooldown clears."""
    handler, system_map, _device = await _handler(_StubCoordinator())
    labware = await create_test_plate_template("plate_1").create_instance()
    thread_id = "thread-1"
    handler._starvation_registry.increment_starvation_score(thread_id)
    handler._recovery_strategy.record_visit(thread_id, "pad_2")

    result = await handler.resolve_move_action(
        thread_id,
        labware,
        system_map.get_location("shaker_1/slot_a"),
        [system_map.get_location("pad_1")],
        escape_on_arrival=False,
    )

    assert result.target.position_id == "pad_1"
    assert handler._starvation_registry.get_starvation_score(thread_id) == 1
    assert handler._recovery_strategy.get_cooldown(thread_id) == {"pad_2"}


@pytest.mark.asyncio
async def test_paths_sharing_a_first_hop_collapse_to_one_move() -> None:
    """Scored paths are per-hop: a move targets ``path[1]``, not the destination.
    Several candidate sites reached through one translator therefore produce
    several paths whose first hop is identical, and one move each would put two
    reservations for that hop in a single collection."""
    handler, system_map, _device = await _handler(_StubCoordinator())
    labware = await create_test_plate_template("plate_1").create_instance()

    paths = [
        ["pad_1", "pad_2", "shaker_1/slot_a"],
        ["pad_1", "pad_2", "shaker_1/slot_b"],
    ]
    moves = handler._get_potential_move_actions(labware, paths)

    assert [m.target.position_id for m in moves] == ["pad_2"], (
        "paths sharing a first hop must yield ONE move for that hop"
    )


@pytest.mark.asyncio
async def test_parking_picker_avoids_a_physically_occupied_pad() -> None:
    """A settled/loose plate holds a pad with NO reservation; the deadlock
    parking picker must still steer around it. Reservation-only avoidance
    commits the collection to a pad that can never grant, and the rejected
    branch retries that single path forever."""
    from tests.test_cross_tick_deadlock import _make_labware, _place_labware

    device = create_test_device("shaker_1", site_names=["slot_a"])
    # Both pads sit on the source's own arm: a park is only ever completed by
    # the move that resolves it, so the alternative has to be one this move can
    # reach or the picker has nothing to steer TO.
    arm_1 = create_test_transporter(
        "arm_1", ["shaker_1/slot_a", "pad_near", "pad_far", "pad_dest"]
    )
    # pad_extra keeps pad_far off the scorer's dead-end penalty so the pick
    # isolates the occupancy question.
    arm_2 = create_test_transporter("arm_2", ["pad_far", "pad_extra"])
    _registry, system_map = await create_simple_system_map(
        [arm_1, arm_2],
        {"shaker_1": device},
        {
            "pad_near": PlatePad("pad_near"),
            "pad_far": PlatePad("pad_far"),
            "pad_extra": PlatePad("pad_extra", supports_deadlock_resolution=False),
            "pad_dest": PlatePad("pad_dest", supports_deadlock_resolution=False),
        },
    )
    handler = MoveHandler(_StubCoordinator(), system_map, DeadlockStarvationRegistry())
    labware = await create_test_plate_template("plate_1").create_instance()
    _place_labware(system_map.get_location("pad_near"), _make_labware("settled"))

    # Blocked target must sit on neither candidate route: the scorer drops
    # every path containing blocked_location before scoring.
    blocked_move = handler._get_potential_move_actions(
        labware, [["shaker_1/slot_a", "pad_dest"]]
    )[0]
    parking = handler._build_parking_move_collection("thread-1", blocked_move)

    assert parking[0].target.position_id != "pad_near", (
        "the nearest pad physically holds another plate (unreserved); "
        "committing the parking collection to it wedges the escape path"
    )
