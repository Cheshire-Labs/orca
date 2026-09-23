"""Reservation .clear() invariant: cannot clear a granted reservation.

Three sites enforce this invariant:

* ``LocationReservation.clear()`` (system/reservation_manager/location_reservation.py)
* ``MoveActionCollectionReservationRequest.clear()`` (system/reservation_manager/move_handler.py)
* ``LocationCollectionReservationRequest.clear()`` (workflow_models/actions/util.py)

Each one was previously labeled "I haven't looked into the implications yet"
and raised ``ValueError``. The implication is now defined: a granted
reservation owns a live location lock; clearing it without releasing the
lock orphans the lock. The retry paths only call clear() on deadlocked or
rejected branches, so a granted collection at clear() time signals caller
misuse. These tests pin that invariant.
"""
from unittest.mock import MagicMock

import pytest

from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import (
    MoveActionCollectionReservationRequest,
)
from orca.workflow_models.actions.util import LocationCollectionReservationRequest


def _make_location(name: str = "loc-1") -> Location:
    location = MagicMock(spec=Location)
    location.name = name
    location.position_id = name
    return location


class TestLocationReservationClearInvariant:
    def test_clear_succeeds_when_not_granted(self) -> None:
        reservation = LocationReservation(_make_location(), None)
        assert not reservation.granted.is_set()
        reservation.clear()  # no raise

    def test_clear_raises_runtime_error_when_granted(self) -> None:
        reservation = LocationReservation(_make_location(), None)
        reservation.granted.set()
        with pytest.raises(RuntimeError, match="cannot clear a granted reservation"):
            reservation.clear()


class TestMoveActionCollectionReservationRequestClearInvariant:
    def test_clear_succeeds_when_not_granted(self) -> None:
        collection = MoveActionCollectionReservationRequest("thread-1", [])
        assert not collection.granted.is_set()
        collection.clear()  # no raise

    def test_clear_raises_runtime_error_when_granted(self) -> None:
        collection = MoveActionCollectionReservationRequest("thread-1", [])
        collection.granted.set()
        with pytest.raises(RuntimeError, match="cannot clear a granted collection"):
            collection.clear()


class TestLocationCollectionReservationRequestClearInvariant:
    def test_clear_succeeds_when_not_granted(self) -> None:
        collection = LocationCollectionReservationRequest(
            "thread-1", [], MagicMock(), _make_location(),
        )
        assert not collection.granted.is_set()
        collection.clear()  # no raise

    def test_clear_raises_runtime_error_when_granted(self) -> None:
        collection = LocationCollectionReservationRequest(
            "thread-1", [], MagicMock(), _make_location(),
        )
        collection.granted.set()
        with pytest.raises(RuntimeError, match="cannot clear a granted collection"):
            collection.clear()
