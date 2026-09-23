"""Unit tests for reservation cancellation at the manager + coordinator layers.

These bypass the HTTP route and validate the core semantics:
- `LocationReservationManager.release_reservation_by_id`: scans reservations
  by id, frees the one that matches, fires the release callback.
- `ThreadReservationCoordinator.cancel_reservation_by_id`: returns
  (position_id, thread_id) and raises `KeyError` for unknown ids.

The execution-scoping check (does this reservation belong to the caller's
execution?) lives one layer up in `SystemRuntime.cancel_reservation`; see
`test_routes_execution_remove_and_reservation_cancel.py` for the route-level coverage.
"""

from unittest.mock import Mock

import pytest

from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
    ThreadReservationCoordinator,
)


def _make_location(name: str, occupied: bool = False) -> Mock:
    loc = Mock(spec=Location)
    loc.name = name
    loc.labware = Mock() if occupied else None
    return loc


def _make_location_reg(locations: dict[str, Mock]) -> Mock:
    reg = Mock()
    reg.get_location.side_effect = lambda name: locations[name]
    return reg


class TestLocationReservationManagerReleaseById:
    def test_release_by_id_removes_reservation_and_returns_location(self) -> None:
        loc = _make_location("pad1")
        reg = _make_location_reg({"pad1": loc})
        mgr = LocationReservationManager(reg)

        reservation = LocationReservation(loc)
        mgr._reserve("pad1", reservation, thread_id="t1")

        freed_loc = mgr.release_reservation_by_id(reservation.id)

        assert freed_loc == "pad1"
        assert "pad1" not in mgr.reservations, (
            "release_reservation_by_id must remove the reservation from the "
            "manager's dict"
        )

    def test_release_by_id_returns_none_for_unknown_id(self) -> None:
        loc = _make_location("pad1")
        reg = _make_location_reg({"pad1": loc})
        mgr = LocationReservationManager(reg)

        # No reservations held; nothing to release.
        assert mgr.release_reservation_by_id("does-not-exist") is None

    def test_release_by_id_fires_on_release_callback(self) -> None:
        loc = _make_location("pad1")
        reg = _make_location_reg({"pad1": loc})
        mgr = LocationReservationManager(reg)

        reservation = LocationReservation(loc)
        mgr._reserve("pad1", reservation, thread_id="t1")

        callback = Mock()
        mgr.set_on_release_callback(callback)

        mgr.release_reservation_by_id(reservation.id)
        callback.assert_called_once()

    def test_release_by_id_leaves_other_reservations_untouched(self) -> None:
        loc1 = _make_location("pad1")
        loc2 = _make_location("pad2")
        reg = _make_location_reg({"pad1": loc1, "pad2": loc2})
        mgr = LocationReservationManager(reg)

        r1 = LocationReservation(loc1)
        r2 = LocationReservation(loc2)
        mgr._reserve("pad1", r1, thread_id="t1")
        mgr._reserve("pad2", r2, thread_id="t2")

        mgr.release_reservation_by_id(r1.id)

        assert "pad1" not in mgr.reservations
        assert "pad2" in mgr.reservations, (
            "releasing one reservation must not touch unrelated ones"
        )
        assert mgr.reservations["pad2"].id == r2.id


class TestThreadReservationCoordinatorCancel:
    def test_cancel_returns_location_and_thread_id(self) -> None:
        loc = _make_location("pad1")
        reg = _make_location_reg({"pad1": loc})
        thread_registry = Mock()
        coordinator = ThreadReservationCoordinator(reg, thread_registry)

        # Directly stage a granted reservation into the underlying manager.
        reservation = LocationReservation(loc)
        coordinator._reservation_manager._reserve("pad1", reservation, thread_id="t1")

        position_id, thread_id = coordinator.cancel_reservation_by_id(reservation.id)

        assert position_id == "pad1"
        assert thread_id == "t1"

    def test_cancel_removes_reservation_from_active_list(self) -> None:
        loc = _make_location("pad1")
        reg = _make_location_reg({"pad1": loc})
        coordinator = ThreadReservationCoordinator(reg, Mock())

        reservation = LocationReservation(loc)
        coordinator._reservation_manager._reserve("pad1", reservation, thread_id="t1")
        assert any(
            r[1] == reservation.id for r in coordinator.get_active_reservations()
        ), "reservation must be listed before cancel"

        coordinator.cancel_reservation_by_id(reservation.id)

        assert not any(
            r[1] == reservation.id for r in coordinator.get_active_reservations()
        ), "cancelled reservation must disappear from the active list"

    def test_cancel_unknown_id_raises_key_error(self) -> None:
        reg = _make_location_reg({})
        coordinator = ThreadReservationCoordinator(reg, Mock())

        with pytest.raises(KeyError, match="nope"):
            coordinator.cancel_reservation_by_id("nope")

    def test_cancel_frees_location_for_reuse(self) -> None:
        """After cancel, `can_reserve` must return True for the freed location
        (assuming the location has no labware). This is what makes the hammer
        actually useful: someone else can now claim the slot."""
        loc = _make_location("pad1")
        reg = _make_location_reg({"pad1": loc})
        coordinator = ThreadReservationCoordinator(reg, Mock())

        reservation = LocationReservation(loc)
        coordinator._reservation_manager._reserve("pad1", reservation, thread_id="t1")
        assert coordinator._reservation_manager.can_reserve("pad1") is False

        coordinator.cancel_reservation_by_id(reservation.id)

        assert coordinator._reservation_manager.can_reserve("pad1") is True
