"""A parking offer must be a pad this move can actually reach.

Moves are resolved one hop at a time and the park intent is not carried
across hops: the thread executes the first hop, then re-resolves toward its
REAL target. So a pad more than one hop away is never reached. Offering one
does not merely waste a move -- it parks the plate on the transit position in
between, which the next resolution immediately moves it off again. Six
concurrent SMC submissions oscillated a plate between the two bridge
endpoints of a zone that way, squatting the crossings every other thread
needed, because the only free resolution pads were in the next zone.
"""
from typing import Dict, Iterable, List

import pytest

from orca.config import ReservationConfig
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


class _FlagThenBlockCoordinator(IThreadReservationCoordinator):
    """Flags the first submission deadlocked, then grants only ``grantable``."""

    def __init__(self, grantable: set[str]) -> None:
        self._grantable = grantable
        self.asked: List[set[str]] = []

    async def submit_reservation_request(
        self, thread_id: str, request: IReservationCollection
    ) -> None:
        self.asked.append(
            {r.requested_location.position_id for r in request.get_reservations()}
        )
        if len(self.asked) == 1:
            request.deadlocked.set()
            request.processed.set()
            return
        for reservation in request.get_reservations():
            if reservation.requested_location.position_id in self._grantable:
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


async def _two_zone_map(pad_is_one_hop: bool) -> SystemMap:
    """Two arms joined by a bridge pad, with the only resolution pad on one side.

    ``pad_is_one_hop``: the pad shares the source's arm (reachable in this
    move) or sits across the bridge (reachable only by a later hop the park
    never gets to make). The bridge itself is park-ineligible, exactly like the
    translator endpoints in the SMC topology.
    """
    target_device = create_test_device("shaker_1", site_names=["slot_a"])
    source_device = create_test_device("shaker_2", site_names=["slot_a"])
    near_side = ["shaker_1/slot_a", "shaker_2/slot_a", "bridge"]
    far_side = ["bridge", "far_room"]
    if pad_is_one_hop:
        near_side.append("pad_park")
    else:
        far_side.append("pad_park")
    arm_1 = create_test_transporter("arm_1", near_side)
    arm_2 = create_test_transporter("arm_2", far_side)
    _registry, system_map = await create_simple_system_map(
        [arm_1, arm_2],
        {"shaker_1": target_device, "shaker_2": source_device},
        {
            "bridge": PlatePad("bridge", supports_deadlock_resolution=False),
            "far_room": PlatePad("far_room", supports_deadlock_resolution=False),
            "pad_park": PlatePad("pad_park"),
        },
    )
    return system_map


async def _resolve(system_map: SystemMap, coordinator: _FlagThenBlockCoordinator):
    handler = MoveHandler(
        coordinator,
        system_map,
        DeadlockStarvationRegistry(),
        ReservationConfig(move_reservation_timeout=3.0, retry_interval=0.05),
    )
    labware = await create_test_plate_template("plate_1").create_instance()
    return await handler.resolve_move_action(
        "thread-1",
        labware,
        system_map.get_location("shaker_2/slot_a"),
        [system_map.get_location("shaker_1/slot_a")],
    )


@pytest.mark.asyncio
async def test_a_pad_across_a_bridge_is_not_offered_as_a_park() -> None:
    """The transit position on the way to a far pad must never be the offer."""
    coordinator = _FlagThenBlockCoordinator(grantable={"shaker_1/slot_a"})
    system_map = await _two_zone_map(pad_is_one_hop=False)

    await _resolve(system_map, coordinator)

    assert len(coordinator.asked) >= 2, "expected a resubmission after the flag"
    assert "bridge" not in coordinator.asked[1], (
        "the deadlock resolver offered the transit hop on the way to a pad it "
        "cannot reach; the plate parks on the crossing and bounces straight back"
    )


@pytest.mark.asyncio
async def test_a_pad_on_the_same_arm_is_still_offered_as_a_park() -> None:
    """Parking that CAN complete stays: the fix must not disable the yield."""
    coordinator = _FlagThenBlockCoordinator(grantable={"shaker_1/slot_a"})
    system_map = await _two_zone_map(pad_is_one_hop=True)

    await _resolve(system_map, coordinator)

    assert "pad_park" in coordinator.asked[1], (
        "a pad one hop away is a park the move can complete and must be offered"
    )


@pytest.mark.asyncio
async def test_every_reachable_pad_is_offered_not_one_gamble() -> None:
    """The park offer is pick-one, so it must list every pad it could reach.

    Committing the collection to a single best-scored pad is a bet on that pad
    freeing. When it does not, and a sibling pad does, nothing re-picks: the
    six-concurrent SMC run deadlocked head-on across a bridge with a free pad
    waiting on each side.
    """
    coordinator = _FlagThenBlockCoordinator(grantable={"pad_far"})
    target_device = create_test_device("shaker_1", site_names=["slot_a"])
    source_device = create_test_device("shaker_2", site_names=["slot_a"])
    arm_1 = create_test_transporter(
        "arm_1", ["shaker_1/slot_a", "shaker_2/slot_a", "pad_near", "pad_far"]
    )
    # A second arm on each pad keeps both off the scorer's dead-end penalty,
    # so the pick is about availability and nothing else.
    arm_2 = create_test_transporter("arm_2", ["pad_near", "pad_far", "far_room"])
    _registry, system_map = await create_simple_system_map(
        [arm_1, arm_2],
        {"shaker_1": target_device, "shaker_2": source_device},
        {
            "pad_near": PlatePad("pad_near"),
            "pad_far": PlatePad("pad_far"),
            "far_room": PlatePad("far_room", supports_deadlock_resolution=False),
        },
    )
    handler = MoveHandler(
        coordinator,
        system_map,
        DeadlockStarvationRegistry(),
        ReservationConfig(move_reservation_timeout=3.0, retry_interval=0.05),
    )
    labware = await create_test_plate_template("plate_1").create_instance()

    result = await handler.resolve_move_action(
        "thread-1",
        labware,
        system_map.get_location("shaker_2/slot_a"),
        [system_map.get_location("shaker_1/slot_a")],
    )

    assert {"pad_near", "pad_far"} <= coordinator.asked[1], (
        "both pads are one hop away; offering only the best-scored one bets "
        f"the escape on it freeing. Asked for {sorted(coordinator.asked[1])}"
    )
    assert result.target.position_id == "pad_far"
