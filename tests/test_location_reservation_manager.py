"""
Unit tests for LocationReservationManager class.
These tests serve as a safety net before Phase 2 refactoring.
"""
import pytest
import asyncio
from unittest.mock import Mock

from orca.system.reservation_manager.reservation_manager import LocationReservationManager
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.resource_models.location import Location


class TestLocationReservationManager:
    """Test suite for LocationReservationManager class"""

    def test_can_reserve_empty_unreserved_location(self):
        """Test that can_reserve returns True for empty and unreserved location"""
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location that's empty
        mock_location = Mock(spec=Location)
        mock_location.labware = None
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Verify can reserve
        assert manager.can_reserve("test_location") is True

    def test_cannot_reserve_occupied_location(self):
        """Test that can_reserve returns False when location has labware"""
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location with labware (occupied)
        mock_location = Mock(spec=Location)
        mock_location.labware = Mock()  # Occupied
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Verify cannot reserve
        assert manager.can_reserve("test_location") is False

    def test_cannot_reserve_already_reserved_location(self):
        """Test that can_reserve returns False when location is already reserved"""
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location that's empty
        mock_location = Mock(spec=Location)
        mock_location.labware = None
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Make first reservation
        reservation1 = LocationReservation(mock_location)
        manager._reserve("test_location", reservation1)

        # Try to check if we can reserve again - should return False
        assert manager.can_reserve("test_location") is False

    @pytest.mark.asyncio
    async def test_attempt_reservation_success(self):
        """Test that attempt_reservation grants when location is available"""
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location that's empty
        mock_location = Mock(spec=Location)
        mock_location.labware = None
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Create reservation request
        reservation = LocationReservation(mock_location)

        # Attempt reservation
        await manager.attempt_reservation("test_location", reservation)

        # Verify granted and processed are set
        assert reservation.granted.is_set()
        assert reservation.processed.is_set()
        assert not reservation.rejected.is_set()

    @pytest.mark.asyncio
    async def test_attempt_reservation_rejected(self):
        """Test that attempt_reservation rejects when location is occupied"""
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location with labware (occupied)
        mock_location = Mock(spec=Location)
        mock_location.labware = Mock()  # Occupied
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Create reservation request
        reservation = LocationReservation(mock_location)

        # Attempt reservation
        await manager.attempt_reservation("test_location", reservation)

        # Verify rejected and processed are set
        assert reservation.rejected.is_set()
        assert reservation.processed.is_set()
        assert not reservation.granted.is_set()

    def test_release_reservation_removes_from_dict(self):
        """Test that release_reservation removes reservation from internal dict"""
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location that's empty
        mock_location = Mock(spec=Location)
        mock_location.labware = None
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Make reservation
        reservation = LocationReservation(mock_location)
        manager._reserve("test_location", reservation)

        # Verify it's in the dict
        assert "test_location" in manager.reservations

        # Release reservation
        manager.release_reservation("test_location")

        # Verify it's removed from dict
        assert "test_location" not in manager.reservations

    def test_get_reservation_at(self):
        """Test get_reservation_at returns correct reservation or None"""
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location that's empty
        mock_location = Mock(spec=Location)
        mock_location.labware = None
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Test with no reservation
        assert manager.get_reservation_at("test_location") is None

        # Make reservation
        reservation = LocationReservation(mock_location)
        manager._reserve("test_location", reservation)

        # Test that we get the reservation back
        retrieved = manager.get_reservation_at("test_location")
        assert retrieved == reservation

        # Test non-existent location returns None
        assert manager.get_reservation_at("nonexistent") is None
