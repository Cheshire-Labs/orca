"""
Unit tests for LocationReservation class.
These tests serve as a safety net before Phase 2 refactoring.
"""
import pytest
import asyncio
from unittest.mock import Mock

from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.resource_models.location import Location
from orca.resource_models.labware import LabwareInstance


class TestLocationReservation:
    """Test suite for LocationReservation class"""

    def test_reservation_creation(self):
        """Test that LocationReservation can be created with proper initial state"""
        mock_location = Mock(spec=Location)
        mock_location.name = "test_location"

        reservation = LocationReservation(mock_location)

        # Verify ID is generated
        assert reservation.id is not None
        assert isinstance(reservation.id, str)

        # Verify events are not set initially
        assert not reservation.granted.is_set()
        assert not reservation.rejected.is_set()
        assert not reservation.deadlocked.is_set()
        assert not reservation.processed.is_set()

        # Verify requested location is set
        assert reservation.requested_location == mock_location

    def test_set_location_and_retrieve(self):
        """Test setting and retrieving reserved location"""
        mock_requested = Mock(spec=Location)
        mock_requested.name = "requested"
        mock_reserved = Mock(spec=Location)
        mock_reserved.name = "reserved"

        reservation = LocationReservation(mock_requested)
        reservation.set_location(mock_reserved)

        # Verify reserved location returns the set location
        assert reservation.reserved_location == mock_reserved

    def test_reservation_events(self):
        """Test that reservation events can be set and queried"""
        mock_location = Mock(spec=Location)
        reservation = LocationReservation(mock_location)

        # Test granted event
        reservation.granted.set()
        assert reservation.granted.is_set()

        # Test rejected event
        reservation.rejected.set()
        assert reservation.rejected.is_set()

        # Test deadlocked event
        reservation.deadlocked.set()
        assert reservation.deadlocked.is_set()

        # Test processed event
        reservation.processed.set()
        assert reservation.processed.is_set()

    def test_reserved_location_before_set_raises(self):
        """Test that accessing reserved_location before setting raises ValueError"""
        mock_location = Mock(spec=Location)
        reservation = LocationReservation(mock_location)

        with pytest.raises(ValueError) as exc_info:
            _ = reservation.reserved_location

        assert "Location not yet reserved" in str(exc_info.value)

    def test_clear_resets_events(self):
        """Test that clear() resets the appropriate events"""
        mock_location = Mock(spec=Location)
        reservation = LocationReservation(mock_location)

        # Set some events
        reservation.deadlocked.set()
        reservation.rejected.set()
        reservation.processed.set()

        # Clear
        reservation.clear()

        # Verify events are cleared
        assert not reservation.deadlocked.is_set()
        assert not reservation.rejected.is_set()
        assert not reservation.processed.is_set()

    def test_release_callback_triggers(self):
        """Test that release callback is called when release_reservation is invoked"""
        mock_location = Mock(spec=Location)
        reservation = LocationReservation(mock_location)

        # Set up mock callback
        callback = Mock()
        reservation.set_reservation_release_callback(callback)

        # Trigger release
        reservation.release_reservation()

        # Verify callback was called once
        callback.assert_called_once()

    def test_release_without_callback_no_error(self):
        """Test that calling release_reservation without setting callback doesn't error"""
        mock_location = Mock(spec=Location)
        reservation = LocationReservation(mock_location)

        # This should not raise an error (default lambda does nothing)
        reservation.release_reservation()

    def test_clear_granted_raises_valueerror(self):
        """Test that clearing a granted reservation raises ValueError"""
        mock_location = Mock(spec=Location)
        reservation = LocationReservation(mock_location)

        # Grant the reservation
        reservation.granted.set()

        # Try to clear - should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            reservation.clear()

        assert "Cannot clear a granted reservation" in str(exc_info.value)
