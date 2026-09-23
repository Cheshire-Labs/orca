"""Tests for deadlock recovery livelock prevention.

Tests the DeadlockRecoveryStrategy which prevents two threads from
endlessly bouncing between the same parking pads during deadlock
resolution. Two mechanisms: occupied-pad penalty and per-thread cooldown.
"""

import pytest

from orca.system.reservation_manager.path_scoring import (
    PathScoringStrategy,
    PathScoringWeights,
)
from orca.system.reservation_manager.deadlock_recovery import (
    DeadlockRecoveryStrategy,
)


class TestOccupiedPadPenalty:
    """Occupied-pad penalty in PathScoringStrategy."""

    def test_no_penalty_when_no_occupied_locations(self) -> None:
        """Paths score the same as before when no locations are occupied."""
        weights = PathScoringWeights()
        paths = [["src", "pad_7"], ["src", "pad_8"]]
        scored = _score_with_occupied(weights, paths, occupied=None)
        assert scored[0].occupied_penalty == 0.0
        assert scored[1].occupied_penalty == 0.0

    def test_penalty_applied_to_occupied_destination(self) -> None:
        """A path ending at an occupied pad gets the penalty."""
        weights = PathScoringWeights()
        paths = [["src", "pad_7"], ["src", "pad_8"]]
        scored = _score_with_occupied(weights, paths, occupied={"pad_7"})
        pad7_score = next(s for s in scored if s.path[-1] == "pad_7")
        pad8_score = next(s for s in scored if s.path[-1] == "pad_8")
        assert pad7_score.occupied_penalty == weights.occupied_pad_penalty
        assert pad8_score.occupied_penalty == 0.0

    def test_occupied_pad_ranks_lower(self) -> None:
        """An occupied pad ranks below a free pad even if it's shorter."""
        weights = PathScoringWeights()
        paths = [["src", "pad_7"], ["src", "intermediate", "pad_8"]]
        scored = _score_with_occupied(weights, paths, occupied={"pad_7"})
        # pad_8 is longer (3 hops) but pad_7 is occupied (+50)
        # pad_7 total: 2 * 1.0 + 50.0 = 52.0
        # pad_8 total: 3 * 1.0 + 0.0 = 3.0
        assert scored[0].path[-1] == "pad_8", (
            f"Free pad should rank first but got: {[s.path[-1] for s in scored]}"
        )

    def test_all_occupied_still_produces_valid_ranking(self) -> None:
        """When all pads are occupied, scoring still works (penalty is additive)."""
        weights = PathScoringWeights()
        paths = [["src", "pad_7"], ["src", "intermediate", "pad_8"]]
        scored = _score_with_occupied(weights, paths, occupied={"pad_7", "pad_8"})
        # Both penalized, shorter path wins
        assert scored[0].path[-1] == "pad_7"


class TestDeadlockRecoveryStrategy:
    """DeadlockRecoveryStrategy cooldown and avoidance logic."""

    def test_initial_cooldown_empty(self) -> None:
        strategy = DeadlockRecoveryStrategy()
        assert strategy.get_cooldown("thread_1") == set()

    def test_record_visit_adds_to_cooldown(self) -> None:
        strategy = DeadlockRecoveryStrategy()
        strategy.record_visit("thread_1", "pad_7")
        assert "pad_7" in strategy.get_cooldown("thread_1")

    def test_cooldown_accumulates(self) -> None:
        strategy = DeadlockRecoveryStrategy()
        strategy.record_visit("thread_1", "pad_7")
        strategy.record_visit("thread_1", "pad_8")
        assert strategy.get_cooldown("thread_1") == {"pad_7", "pad_8"}

    def test_cooldown_per_thread(self) -> None:
        strategy = DeadlockRecoveryStrategy()
        strategy.record_visit("thread_1", "pad_7")
        strategy.record_visit("thread_2", "pad_8")
        assert strategy.get_cooldown("thread_1") == {"pad_7"}
        assert strategy.get_cooldown("thread_2") == {"pad_8"}

    def test_clear_cooldown(self) -> None:
        strategy = DeadlockRecoveryStrategy()
        strategy.record_visit("thread_1", "pad_7")
        strategy.record_visit("thread_1", "pad_8")
        strategy.clear_cooldown("thread_1")
        assert strategy.get_cooldown("thread_1") == set()

    def test_clear_cooldown_nonexistent_thread(self) -> None:
        """Clearing cooldown for unknown thread does not raise."""
        strategy = DeadlockRecoveryStrategy()
        strategy.clear_cooldown("nonexistent")

    def test_get_locations_to_avoid_combines_reserved_and_cooldown(self) -> None:
        strategy = DeadlockRecoveryStrategy()
        strategy.record_visit("thread_1", "pad_7")
        reserved_by_others = {"pad_8", "pad_9"}
        avoid = strategy.get_locations_to_avoid("thread_1", reserved_by_others)
        assert avoid == {"pad_7", "pad_8", "pad_9"}

    def test_get_locations_to_avoid_no_overlap(self) -> None:
        strategy = DeadlockRecoveryStrategy()
        strategy.record_visit("thread_1", "pad_7")
        reserved_by_others = {"pad_7"}  # same pad in both
        avoid = strategy.get_locations_to_avoid("thread_1", reserved_by_others)
        assert avoid == {"pad_7"}  # no duplication


class TestGetReservedLocationNames:
    """ThreadReservationCoordinator.get_reserved_position_ids."""

    # These tests will be added after the method is implemented.
    # They need the full coordinator setup which requires ILocationRegistry
    # and IThreadRegistry mocks. For now, tested via integration tests.
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score_with_occupied(
    weights: PathScoringWeights,
    paths: list[list[str]],
    occupied: set[str] | None,
) -> list:
    """Score paths with occupied_locations using PathScoringStrategy.

    Uses a minimal mock SystemMap since scoring doesn't need real routing.
    """
    from unittest.mock import MagicMock
    mock_map = MagicMock()
    scorer = PathScoringStrategy(mock_map, weights)
    return scorer.score_paths(
        paths,
        thread_id="test_thread",
        starvation_score=0,
        occupied_locations=occupied,
    )
