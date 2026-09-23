"""A deadlock-flagged move must keep asking for its real target.

Parking is a YIELD, not a replacement destination. When the resolver diverts a
flagged thread to a resolution pad it must ADD the pad to the collection, the
way the site-vacate widening does, so the real target still wins the moment it
frees. Replacing the collection with a park-only one strands the thread if the
pad cannot be granted: it never asks for its destination again, and no later
release can wake it. That is how six concurrent SMC submissions wedged with
every resolution pad occupied and zero executions terminal.
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


class _PadBlockedCoordinator(IThreadReservationCoordinator):
    """Flags the first submission deadlocked, then grants only ``grantable``.

    Stands in for a deck whose resolution pads are all occupied: the pad can
    never be reserved, so a park-only collection can never be granted either.
    ``asked`` records the positions of every submitted collection.
    """

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


async def _handler(
    coordinator: IThreadReservationCoordinator,
) -> tuple[MoveHandler, SystemMap]:
    """Source on one device's site, target on another's, one pad between them.

    The plate sits on a working site, so parking is legitimately offered; the
    pad is the only resolution location the route can reach.
    """
    target_device = create_test_device("shaker_1", site_names=["slot_a"])
    source_device = create_test_device("shaker_2", site_names=["slot_a"])
    arm = create_test_transporter(
        "arm", ["shaker_1/slot_a", "shaker_2/slot_a", "pad_park"]
    )
    _registry, system_map = await create_simple_system_map(
        [arm],
        {"shaker_1": target_device, "shaker_2": source_device},
        {"pad_park": PlatePad("pad_park")},
    )
    handler = MoveHandler(
        coordinator,
        system_map,
        DeadlockStarvationRegistry(),
        ReservationConfig(move_reservation_timeout=3.0, retry_interval=0.05),
    )
    return handler, system_map


@pytest.mark.asyncio
async def test_deadlock_park_still_reaches_a_target_no_pad_can_be_had() -> None:
    """The real target wins when the pad is unreachable, instead of hanging."""
    coordinator = _PadBlockedCoordinator(grantable={"shaker_1/slot_a"})
    handler, system_map = await _handler(coordinator)
    labware = await create_test_plate_template("plate_1").create_instance()

    result = await handler.resolve_move_action(
        "thread-1",
        labware,
        system_map.get_location("shaker_2/slot_a"),
        [system_map.get_location("shaker_1/slot_a")],
    )

    assert result.target.position_id == "shaker_1/slot_a", (
        "a flagged move that cannot park must still take its real target"
    )


@pytest.mark.asyncio
async def test_parking_collection_keeps_the_real_target_alongside_the_pad() -> None:
    """The post-flag collection asks for BOTH: the pad is a yield offer, and
    dropping the destination is what makes an ungrantable pad terminal."""
    coordinator = _PadBlockedCoordinator(grantable={"shaker_1/slot_a"})
    handler, system_map = await _handler(coordinator)
    labware = await create_test_plate_template("plate_1").create_instance()

    await handler.resolve_move_action(
        "thread-1",
        labware,
        system_map.get_location("shaker_2/slot_a"),
        [system_map.get_location("shaker_1/slot_a")],
    )

    assert len(coordinator.asked) >= 2, "expected a resubmission after the flag"
    assert "shaker_1/slot_a" in coordinator.asked[1], (
        "the collection submitted after a deadlock flag dropped the real "
        f"target; it asked only for {sorted(coordinator.asked[1])}"
    )


@pytest.mark.asyncio
async def test_a_flag_with_no_reachable_pad_keeps_asking_for_the_target() -> None:
    """No pad to yield with is not a reason to fail the move.

    A topology whose route reaches no resolution location used to turn a
    transient deadlock flag into a move failure and an operator pause.
    """
    coordinator = _PadBlockedCoordinator(grantable={"shaker_1/slot_a"})
    target_device = create_test_device("shaker_1", site_names=["slot_a"])
    source_device = create_test_device("shaker_2", site_names=["slot_a"])
    arm = create_test_transporter("arm", ["shaker_1/slot_a", "shaker_2/slot_a"])
    _registry, system_map = await create_simple_system_map(
        [arm], {"shaker_1": target_device, "shaker_2": source_device}, {},
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

    assert result.target.position_id == "shaker_1/slot_a"
