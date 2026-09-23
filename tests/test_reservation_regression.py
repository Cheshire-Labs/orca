"""
Regression tests for reservation system bug fixes.

These tests validate that critical bugs identified in the reservation system
have been properly fixed and do not regress.
"""
import asyncio
import pytest
from unittest.mock import Mock, MagicMock, patch

from tests.test_helpers import wait_until

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
        task1 = asyncio.create_task(coordinator.start_tick_loop())

        # Wait for the loop to mark itself started.
        await wait_until(lambda: coordinator.ticker_started is True, timeout=5.0)

        # Verify ticker started
        assert coordinator.ticker_started is True

        # Guard makes the second call return immediately, not loop.
        task2 = asyncio.create_task(coordinator.start_tick_loop())

        # The guarded second call returns on its own; the first loops forever.
        await wait_until(lambda: task2.done(), timeout=5.0)

        # Without the guard both enter while-True and neither finishes alone.
        assert not task1.done(), "first tick loop should still be running"
        assert task2.done(), "guard should make the second start_tick_loop return immediately"
        assert task2.exception() is None
        assert coordinator.ticker_started is True

        # Clean up the surviving loop task.
        task1.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass


class TestBug2UnboundedRecursion:
    """
    Bug #2: Unbounded recursion in retry logic could cause stack overflow.
    Fix: Add max_retries parameter with depth limit.
    """

    @pytest.mark.asyncio
    async def test_recursion_depth_limited(self):
        """
        Test that retry logic has a maximum depth and raises RuntimeError instead of RecursionError.
        Now uses iterative pattern with exponential backoff (no recursion).
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

        # Create a mock move action with a proper reservation
        from orca.system.reservation_manager.location_reservation import LocationReservation
        mock_location = Mock()
        mock_location.name = "test_location"

        mock_reservation = LocationReservation(mock_location)

        mock_move_action = Mock(spec=MoveAction)
        mock_move_action.labware = Mock()
        mock_move_action.labware.id = "test_labware"
        mock_move_action.reservation = mock_reservation
        mock_move_action.onward_seat_reservations = []
        mock_move_action.terminal_reservations = []
        mock_move_action.owned_onward_reservations = []

        # Counter to track retries
        retry_counter = {"count": 0}

        # Create a collection that will always be rejected (until we hit 5 retries to speed up test)
        async def mock_submit(thread_id, collection):
            # Simulate processing
            retry_counter["count"] += 1
            if retry_counter["count"] < 5:
                # Reject all reservations
                for reservation in collection.get_reservations():
                    reservation.rejected.set()
                    reservation.processed.set()
                collection.resolve_final_reservation()
            else:
                # Grant one of the reservations after 5 retries to avoid waiting for all 100
                for reservation in collection.get_reservations():
                    reservation.granted.set()
                    reservation.processed.set()
                    break  # Only grant one
                collection.resolve_final_reservation()

        mock_coordinator.submit_reservation_request = mock_submit

        # This should succeed after several retries (no RecursionError)
        result = await move_handler._resolve_reservation_from_move_action_collection(
            "test_thread",
            [mock_move_action]
        )

        # Verify we got a result (not a RecursionError crash)
        assert result == mock_move_action
        # Verify we actually retried (iterative pattern works)
        assert retry_counter["count"] >= 5

    @pytest.mark.asyncio
    async def test_repeated_deadlocks_do_not_recurse(self):
        """Repeated deadlock events must iterate, not recurse.

        Six concurrent SMC submissions blew the stack in CI when every
        parking-pad attempt also deadlocked: handle_deadlock called
        _resolve_reservation_from_move_action_collection which called
        handle_deadlock and so on, one Python frame per deadlock event.
        Eventually a rich-formatted "Cross-tick deadlock" log line raised
        RecursionError while rendering, and orca's own recursion was past
        the default limit by then. This test pins the iterative shape by
        sampling the Python call stack at each retry: an iterative loop
        keeps the same frame across iterations, recursion grows the stack
        linearly with the deadlock count.
        """
        import inspect

        from orca.system.reservation_manager.path_scoring import PathScore

        mock_coordinator = Mock()
        mock_coordinator.get_reserved_position_ids = Mock(return_value=set())
        mock_system_map = Mock()
        mock_starvation_registry = Mock()
        mock_starvation_registry.get_starvation_score.return_value = 0

        move_handler = MoveHandler(
            mock_coordinator, mock_system_map, mock_starvation_registry
        )

        shared_labware = Mock()
        shared_labware.id = "test_labware"

        source_location = Mock()
        source_location.name = "src"
        source_location.position_id = "src"

        pad_location = Mock()
        pad_location.name = "pad"
        pad_location.position_id = "pad"

        transporter = Mock()

        mock_system_map.get_shortest_paths_to_deadlock_resolution.return_value = [
            ["src", "pad"]
        ]
        mock_system_map.get_location = Mock(
            side_effect=lambda n: source_location if n == "src" else pad_location
        )
        mock_system_map.get_transporter_between.return_value = transporter
        mock_system_map.boarding_onward_positions.return_value = []

        fake_score = PathScore(
            path=["src", "pad"],
            total_score=0.0,
            length_score=0.0,
            starvation_score=0.0,
            backtracking_penalty=0.0,
            dead_end_penalty=0.0,
            occupied_penalty=0.0,
        )
        move_handler._path_scorer = Mock()
        move_handler._path_scorer.score_paths.return_value = [fake_score]

        initial_move = MagicMock(spec=MoveAction)
        initial_move.labware = shared_labware
        initial_move.source = source_location
        initial_move.target = pad_location
        initial_move.reservation = LocationReservation(pad_location, shared_labware)
        initial_move.onward_seat_reservations = []
        initial_move.terminal_reservations = []
        initial_move.owned_onward_reservations = []

        deadlock_count = {"n": 0}
        deadlocks_before_grant = 200
        stack_depths: list[int] = []

        async def mock_submit(thread_id, collection):
            stack_depths.append(len(inspect.stack()))
            if deadlock_count["n"] < deadlocks_before_grant:
                deadlock_count["n"] += 1
                collection.deadlocked.set()
                collection.processed.set()
            else:
                for reservation in collection.get_reservations():
                    reservation.granted.set()
                collection.resolve_final_reservation()

        mock_coordinator.submit_reservation_request = mock_submit

        result = await move_handler._resolve_reservation_from_move_action_collection(
            "test_thread", [initial_move]
        )

        assert result is not None
        assert deadlock_count["n"] == deadlocks_before_grant

        # Iterative resolver reuses the same Python frame across deadlock
        # retries -- stack depth at the submit boundary is constant. Recursion
        # through handle_deadlock would grow the stack by ~2 frames per
        # iteration. Tolerate a small +/-1 jitter (e.g. asyncio task plumbing)
        # but reject any linear growth.
        assert len(stack_depths) == deadlocks_before_grant + 1
        depth_growth = max(stack_depths) - min(stack_depths)
        assert depth_growth <= 2, (
            f"stack grew {depth_growth} frames across {deadlocks_before_grant} "
            f"deadlock retries; iterative resolver should be constant. "
            f"first={stack_depths[0]} last={stack_depths[-1]} "
            f"max={max(stack_depths)} min={min(stack_depths)}"
        )


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
        detector = ThreadDeadlockDetector(mock_thread_reg, starvation_registry, reservation_at=lambda _position_id: None)

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
        detector = ThreadDeadlockDetector(mock_thread_reg, starvation_registry, reservation_at=lambda _position_id: None)

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
        """Clearing a granted MoveActionCollectionReservationRequest is an
        invariant violation: the retry path only calls clear() on
        deadlocked or rejected branches. See test_reservation_clear_invariant.py
        for the canonical guard tests across all three reservation types.
        """
        # Create mock move action
        mock_move = Mock(spec=MoveAction)
        mock_move.labware = Mock()
        mock_move.labware.id = "test"
        mock_move.reservation = Mock(spec=LocationReservation)
        mock_move.onward_seat_reservations = []
        mock_move.terminal_reservations = []
        mock_move.owned_onward_reservations = []

        # Create collection
        collection = MoveActionCollectionReservationRequest("thread1", [mock_move])

        # Grant it
        collection._granted.set()

        # Try to clear - should raise RuntimeError (invariant violation)
        with pytest.raises(RuntimeError) as exc_info:
            collection.clear()

        assert "cannot clear a granted collection" in str(exc_info.value)

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
            mock_move.reservation.is_displaced = False
            mock_move.reservation.release_reservation = Mock()
            mock_move.onward_seat_reservations = []
            mock_move.terminal_reservations = []
            mock_move.owned_onward_reservations = []
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
