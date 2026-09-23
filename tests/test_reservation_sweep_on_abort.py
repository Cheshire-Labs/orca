"""Reservation sweep on abort (Bug 2: orphaned reservations after stop).

The reservation coordinator is system-wide and shared across executions, but
per-thread release only frees reservations bound to a thread's action/move/
holdover fields. A thread cancelled while parked awaiting a grant releases
nothing, and the still-running tick loop can grant a reservation for the
now-dead thread after its release ran -- a permanent orphan keyed by position.

These tests pin the sweep primitives that close it:
- `LocationReservationManager.release_reservations_for_threads`
- `ThreadReservationCoordinator.release_reservations_for_threads`
- `ThreadReservationCoordinator.mark_threads_dead` (tick refuses to grant)
- `ThreadReservationCoordinator.forget_threads` (drops stale deadlock carry)
"""

from unittest.mock import Mock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import (
    MoveActionCollectionReservationRequest,
)
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
    ThreadReservationCoordinator,
)
from orca.system.system_map import ILocationRegistry
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.actions.move_action import MoveAction


def _make_location(name: str) -> Location:
    return Location(name, PlatePad(name, supports_deadlock_resolution=True))


def _make_location_registry(locations: dict[str, Location]) -> ILocationRegistry:
    reg = Mock(spec=ILocationRegistry)
    reg.get_location = Mock(side_effect=lambda name: locations[name])
    reg.locations = list(locations.values())
    return reg


def _make_thread_registry() -> IThreadRegistry:
    reg = Mock(spec=IThreadRegistry)
    reg.get_thread = Mock(side_effect=lambda tid: None)
    reg.threads = []
    return reg


def _make_collection(
    thread_id: str, source: Location, target: Location,
) -> MoveActionCollectionReservationRequest:
    transporter = Mock()
    transporter.name = "mock_transporter"
    move = MoveAction(LabwareInstance(thread_id, "plate"), source, target, transporter)
    return MoveActionCollectionReservationRequest(thread_id, [move])


class TestManagerReleaseForThreads:
    def test_releases_only_matching_thread_ids(self) -> None:
        locs = {"pad1": _make_location("pad1"), "pad2": _make_location("pad2"),
                "pad3": _make_location("pad3")}
        mgr = LocationReservationManager(_make_location_registry(locs))
        mgr._reserve("pad1", LocationReservation(locs["pad1"]), thread_id="t1")
        mgr._reserve("pad2", LocationReservation(locs["pad2"]), thread_id="t2")
        mgr._reserve("pad3", LocationReservation(locs["pad3"]), thread_id="t3")

        released = mgr.release_reservations_for_threads({"t1", "t3"})

        assert set(released) == {"pad1", "pad3"}
        assert "pad1" not in mgr.reservations
        assert "pad3" not in mgr.reservations
        assert "pad2" in mgr.reservations, "unrelated thread's reservation must survive"

    def test_fires_release_callback_per_released_reservation(self) -> None:
        locs = {"pad1": _make_location("pad1"), "pad2": _make_location("pad2")}
        mgr = LocationReservationManager(_make_location_registry(locs))
        mgr._reserve("pad1", LocationReservation(locs["pad1"]), thread_id="t1")
        mgr._reserve("pad2", LocationReservation(locs["pad2"]), thread_id="t1")
        callback = Mock()
        mgr.set_on_release_callback(callback)

        mgr.release_reservations_for_threads({"t1"})

        assert callback.call_count == 2

    def test_no_match_is_a_noop(self) -> None:
        locs = {"pad1": _make_location("pad1")}
        mgr = LocationReservationManager(_make_location_registry(locs))
        mgr._reserve("pad1", LocationReservation(locs["pad1"]), thread_id="t1")

        assert mgr.release_reservations_for_threads({"other"}) == []
        assert "pad1" in mgr.reservations


class TestCoordinatorReleaseForThreads:
    def test_delegates_and_frees_from_active_list(self) -> None:
        locs = {"pad1": _make_location("pad1")}
        coord = ThreadReservationCoordinator(
            _make_location_registry(locs), _make_thread_registry(),
        )
        coord._reservation_manager._reserve(
            "pad1", LocationReservation(locs["pad1"]), thread_id="t1",
        )
        assert any(r[1] for r in coord.get_active_reservations())

        coord.release_reservations_for_threads({"t1"})

        assert coord.get_active_reservations() == []


class TestMarkThreadsDead:
    async def test_tick_grants_for_a_live_thread(self) -> None:
        """Sanity: an empty target is granted on a normal tick (no dead mark)."""
        locs = {"src": _make_location("src"), "dst": _make_location("dst")}
        coord = ThreadReservationCoordinator(
            _make_location_registry(locs), _make_thread_registry(),
        )
        col = _make_collection("t1", locs["src"], locs["dst"])
        await coord.submit_reservation_request("t1", col)
        await coord._on_tick()

        assert col.granted.is_set()
        assert coord._reservation_manager.get_reservation_at("dst") is not None

    async def test_dead_thread_request_is_refused_not_granted(self) -> None:
        """A queued request whose thread was marked dead must not be granted,
        even though the target is free -- this is the leak the still-running
        tick loop would otherwise create after the thread's release ran."""
        locs = {"src": _make_location("src"), "dst": _make_location("dst")}
        coord = ThreadReservationCoordinator(
            _make_location_registry(locs), _make_thread_registry(),
        )
        col = _make_collection("t1", locs["src"], locs["dst"])
        await coord.submit_reservation_request("t1", col)

        coord.mark_threads_dead({"t1"})
        await coord._on_tick()

        assert not col.granted.is_set(), "dead thread must not be granted"
        assert col.processed.is_set(), "resolver must be unblocked"
        assert coord._reservation_manager.get_reservation_at("dst") is None, (
            "no orphan reservation may be created for a dead thread"
        )

    async def test_dead_thread_request_is_not_carried_into_deadlock_detection(self) -> None:
        """A dead thread's rejected request must not enter the deadlock carry.

        Cross-tick detection reads the carry with no liveness check, so a stale
        dead-thread entry there could flag a live thread as the deadlock yielder.
        """
        locs = {"src": _make_location("src"), "dst": _make_location("dst")}
        coord = ThreadReservationCoordinator(
            _make_location_registry(locs), _make_thread_registry(),
        )
        col = _make_collection("t1", locs["src"], locs["dst"])
        await coord.submit_reservation_request("t1", col)

        coord.mark_threads_dead({"t1"})
        await coord._on_tick()

        assert "t1" not in coord._deadlock_detector._rejected_carry, (
            "a dead thread's request must not be carried into cross-tick detection"
        )


class TestForgetThreads:
    def test_clears_rejected_carry_for_thread(self) -> None:
        locs = {"src": _make_location("src"), "dst": _make_location("dst")}
        coord = ThreadReservationCoordinator(
            _make_location_registry(locs), _make_thread_registry(),
        )
        col = _make_collection("t1", locs["src"], locs["dst"])
        coord._deadlock_detector._rejected_carry["t1"] = col

        coord.forget_threads({"t1"})

        assert "t1" not in coord._deadlock_detector._rejected_carry
