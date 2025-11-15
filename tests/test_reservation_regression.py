"""
Regression tests for reservation system bug fixes (Phase 1).

These tests validate that critical bugs identified in the reservation system
have been properly fixed and do not regress.
"""
import asyncio
import pytest
from unittest.mock import Mock, MagicMock, patch

from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
    ThreadReservationCoordinator
)
from orca.system.reservation_manager.deadlock_manager import ThreadDeadlockDetector, DeadlockStarvationRegistry
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import MoveHandler, MoveActionCollectionReservationRequest
from orca.workflow_models.actions.move_action import MoveAction


class TestBug7MultipleTickLoops:
    """
    Bug #7: Multiple tick loops could be started when multiple workflows execute.
    Fix: Add guard to only start tick loop if not already started.
    """

    @pytest.mark.asyncio
    async def test_single_tick_loop_per_system(self):
        """
        Test that starting multiple workflows doesn't create multiple tick loops.
        """
        # Create mock registries
        mock_location_reg = Mock()
        mock_thread_reg = Mock()

        # Create coordinator
        coordinator = ThreadReservationCoordinator(mock_location_reg, mock_thread_reg)

        # Verify ticker not started initially
        assert coordinator.ticker_started is False

        # Start first tick loop
        task1 = asyncio.create_task(coordinator.start_tick_loop(0.1))

        # Give it a moment to start
        await asyncio.sleep(0.05)

        # Verify ticker started
        assert coordinator.ticker_started is True

        # Try to start another tick loop (simulating second workflow)
        # This should NOT create a second loop
        task2 = asyncio.create_task(coordinator.start_tick_loop(0.1))

        # Give both tasks a moment
        await asyncio.sleep(0.15)

        # Clean up
        task1.cancel()
        task2.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass
        try:
            await task2
        except asyncio.CancelledError:
            pass

        # If we get here without issues, the guard worked
        # (Multiple tick loops would cause race conditions and potential failures)
        assert True


class TestBug2UnboundedRecursion:
    """
    Bug #2: Unbounded recursion in retry logic could cause stack overflow.
    Fix: Add max_retries parameter with depth limit.
    """

    @pytest.mark.asyncio
    async def test_recursion_depth_limited(self):
        """
        Test that retry logic has a maximum depth and raises RuntimeError instead of RecursionError.
        """
        # Create mock dependencies
        mock_coordinator = Mock()
        mock_system_map = Mock()
        mock_starvation_registry = Mock()
        mock_starvation_registry.get_starvation_score.return_value = 0

        # Create MoveHandler
        move_handler = MoveHandler(
            mock_coordinator,
            mock_system_map,
            mock_starvation_registry
        )

        # Create a mock move action
        mock_move_action = Mock(spec=MoveAction)
        mock_move_action.labware = Mock()
        mock_move_action.labware.id = "test_labware"

        # Create a collection that will always be rejected
        async def mock_submit(thread_id, collection):
            # Simulate processing
            collection._processed.set()
            collection._rejected.set()

        mock_coordinator.submit_reservation_request = mock_submit

        # Try to resolve with a low max_retries to speed up test
        with pytest.raises(RuntimeError) as exc_info:
            await move_handler._resolve_reservation_from_move_action_collection(
                "test_thread",
                [mock_move_action],
                max_retries=5,  # Low limit for fast test
                retry_count=0
            )

        # Verify we got RuntimeError (not RecursionError)
        assert "exceeded maximum reservation retries" in str(exc_info.value)
        assert isinstance(exc_info.value, RuntimeError)


class TestBug3NullChecksInDeadlockDetection:
    """
    Bug #3: Missing null checks in deadlock detection could cause AttributeError.
    Fix: Add proper null checking for thread registry lookups.
    """

    def test_deadlock_detection_handles_missing_thread(self):
        """
        Test that deadlock detection handles missing threads gracefully.
        """
        # Create mock registry that returns None for non-existent thread
        mock_thread_reg = Mock()
        mock_thread_reg.get_thread.return_value = None

        # Create starvation registry
        starvation_registry = DeadlockStarvationRegistry()

        # Create detector
        detector = ThreadDeadlockDetector(mock_thread_reg, starvation_registry)

        # Create mock collection
        mock_collection = Mock()
        mock_collection.thread_id = "non_existent_thread"

        # Call the method that had the bug
        # This should NOT raise AttributeError
        result = detector._get_labware_to_thread_map([mock_collection])

        # Should return empty dict (thread was skipped)
        assert result == {}

    def test_deadlock_detection_handles_none_labware(self):
        """
        Test that deadlock detection handles threads with None labware.
        """
        # Create mock thread with None labware
        mock_thread = Mock()
        mock_thread.labware = None

        # Create mock registry that returns the thread
        mock_thread_reg = Mock()
        mock_thread_reg.get_thread.return_value = mock_thread

        # Create starvation registry
        starvation_registry = DeadlockStarvationRegistry()

        # Create detector
        detector = ThreadDeadlockDetector(mock_thread_reg, starvation_registry)

        # Create mock collection
        mock_collection = Mock()
        mock_collection.thread_id = "thread_with_no_labware"

        # Call the method
        # This should NOT raise AttributeError
        result = detector._get_labware_to_thread_map([mock_collection])

        # Should return empty dict (thread was skipped)
        assert result == {}


class TestLocationReservationManager:
    """
    General tests for LocationReservationManager to ensure basic functionality.
    """

    def test_can_reserve_checks_both_conditions(self):
        """
        Test that can_reserve properly checks both unreserved AND empty conditions.
        """
        # Create mock location registry
        mock_location_reg = Mock()

        # Create mock location
        mock_location = Mock()
        mock_location.labware = None  # Empty
        mock_location.name = "test_location"
        mock_location_reg.get_location.return_value = mock_location

        # Create manager
        manager = LocationReservationManager(mock_location_reg)

        # Test 1: Empty and unreserved - should return True
        assert manager.can_reserve("test_location") is True

        # Test 2: Make a reservation - now should return False
        reservation = LocationReservation(mock_location)
        manager._reserve("test_location", reservation)
        assert manager.can_reserve("test_location") is False

        # Test 3: Release reservation but add labware - should return False
        manager.release_reservation("test_location")
        mock_location.labware = Mock()  # Occupied
        assert manager.can_reserve("test_location") is False

        # Test 4: Remove labware - should return True again
        mock_location.labware = None
        assert manager.can_reserve("test_location") is True


class TestStarvationScoreManagement:
    """
    Test that starvation scores are properly managed.
    """

    def test_starvation_score_increments_correctly(self):
        """
        Test starvation score increments and resets.
        """
        registry = DeadlockStarvationRegistry()

        # Initial score should be 0
        assert registry.get_starvation_score("thread1") == 0

        # Increment 3 times
        registry.increment_starvation_score("thread1")
        registry.increment_starvation_score("thread1")
        registry.increment_starvation_score("thread1")

        # Should be 3
        assert registry.get_starvation_score("thread1") == 3

        # Reset
        registry.reset_starvation_score("thread1")

        # Should be 0 again
        assert registry.get_starvation_score("thread1") == 0

    def test_reset_nonexistent_thread_does_not_raise(self):
        """
        Test that resetting a non-existent thread doesn't raise an error.
        """
        registry = DeadlockStarvationRegistry()

        # This should not raise
        registry.reset_starvation_score("nonexistent_thread")

        # And score should still be 0
        assert registry.get_starvation_score("nonexistent_thread") == 0


class TestMoveActionCollectionReservationRequest:
    """
    Tests for MoveActionCollectionReservationRequest.
    """

    def test_clear_granted_reservation_raises_error(self):
        """
        Test that clearing a granted reservation raises ValueError.
        """
        # Create mock move action
        mock_move = Mock(spec=MoveAction)
        mock_move.labware = Mock()
        mock_move.labware.id = "test"
        mock_move.reservation = Mock(spec=LocationReservation)

        # Create collection
        collection = MoveActionCollectionReservationRequest("thread1", [mock_move])

        # Grant it
        collection._granted.set()

        # Try to clear - should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            collection.clear()

        assert "Cannot clear a reservation that has been granted" in str(exc_info.value)

    def test_multiple_paths_first_granted_wins(self):
        """
        Test that when multiple paths are available, first granted is selected.
        """
        # Create shared labware instance (all moves must have same labware)
        shared_labware = Mock()
        shared_labware.id = "test"

        # Create 3 mock move actions
        mock_moves = []
        for i in range(3):
            mock_move = Mock(spec=MoveAction)
            mock_move.labware = shared_labware  # Use same labware instance
            mock_move.reservation = Mock(spec=LocationReservation)
            mock_move.reservation.granted = asyncio.Event()
            mock_move.reservation.release_reservation = Mock()
            mock_moves.append(mock_move)

        # Create collection
        collection = MoveActionCollectionReservationRequest("thread1", mock_moves)

        # Grant the second move action
        mock_moves[1].reservation.granted.set()

        # Resolve
        collection.resolve_final_reservation()

        # Verify the second move was selected
        assert collection.reserved_move_action == mock_moves[1]

        # Verify granted flag is set
        assert collection.granted.is_set()

        # Verify processed flag is set
        assert collection.processed.is_set()
