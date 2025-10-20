"""
Path scoring and selection for move resolution and deadlock handling.

This module encapsulates the logic for evaluating and selecting the best path
from multiple candidates based on weighted factors including:
- Path length (shorter is better)
- Starvation score (prioritize starved threads)
- Backtracking avoidance
- Dead-end detection (avoid paths that trap the thread)
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from orca.resource_models.location import Location
from orca.system.system_map import SystemMap
import logging

orca_logger = logging.getLogger("orca")


@dataclass
class PathScoringWeights:
    """Configurable weights for different path selection factors."""
    path_length: float = 1.0          # Weight for path length (lower is better)
    starvation_priority: float = 10.0  # Weight for starvation score (higher score = higher priority)
    backtracking_penalty: float = 5.0  # Penalty for backtracking paths
    dead_end_penalty: float = 100.0    # Penalty for dead-end parking pads


@dataclass
class PathScore:
    """Scored path with breakdown of contributing factors."""
    path: List[str]
    total_score: float
    length_score: float
    starvation_score: float
    backtracking_penalty: float
    dead_end_penalty: float

    def __repr__(self) -> str:
        return (f"PathScore(path={' -> '.join(self.path)}, "
                f"total={self.total_score:.2f}, "
                f"length={self.length_score:.2f}, "
                f"starvation={self.starvation_score:.2f}, "
                f"backtrack={self.backtracking_penalty:.2f}, "
                f"deadend={self.dead_end_penalty:.2f})")


class PathScoringStrategy:
    """
    Evaluates and scores paths for move selection.

    This strategy encapsulates all path selection logic, making it:
    - Testable in isolation
    - Configurable via weights
    - Easy to extend with new factors
    """

    def __init__(
        self,
        system_map: SystemMap,
        weights: Optional[PathScoringWeights] = None
    ):
        self._system_map = system_map
        self._weights = weights or PathScoringWeights()

    def score_paths(
        self,
        paths: List[List[str]],
        thread_id: str,
        starvation_score: int,
        previous_location: Optional[Location] = None,
        original_target: Optional[Location] = None,
        blocked_location: Optional[Location] = None,
    ) -> List[PathScore]:
        """
        Score all candidate paths and return them sorted by score (best first).

        Args:
            paths: List of candidate paths (each path is list of location names)
            thread_id: ID of the thread requesting the move
            starvation_score: Current starvation score for this thread
            previous_location: Previous location (for backtracking detection)
            original_target: Original target location (for dead-end detection during deadlock)
            blocked_location: Location that is blocked (filter out paths containing it)

        Returns:
            List of PathScore objects sorted by total_score (lower is better)
        """
        # Filter out paths containing the blocked location (if specified)
        if blocked_location is not None:
            filtered_paths = [
                path for path in paths
                if blocked_location.teachpoint_name not in path
            ]
            if filtered_paths:
                paths = filtered_paths
            # else: keep original paths (all contain blocked location)

        scored_paths = []

        for path in paths:
            score = self._score_single_path(
                path,
                thread_id,
                starvation_score,
                previous_location,
                original_target
            )
            scored_paths.append(score)

        # Sort by total score (lower is better)
        scored_paths.sort(key=lambda x: x.total_score)

        return scored_paths

    def _score_single_path(
        self,
        path: List[str],
        thread_id: str,
        starvation_score: int,
        previous_location: Optional[Location],
        original_target: Optional[Location],
    ) -> PathScore:
        """Score a single path based on all factors."""

        # Factor 1: Path length (shorter is better)
        length_score = len(path) * self._weights.path_length

        # Factor 2: Starvation priority (higher starvation = lower penalty)
        # Invert starvation score so higher starvation reduces total score
        starvation_penalty = -starvation_score * self._weights.starvation_priority

        # Factor 3: Backtracking penalty
        backtracking_penalty = self._calculate_backtracking_penalty(path, previous_location)

        # Factor 4: Dead-end penalty (for deadlock resolution)
        dead_end_penalty = self._calculate_dead_end_penalty(path, original_target)

        total_score = (
            length_score +
            starvation_penalty +
            backtracking_penalty +
            dead_end_penalty
        )

        return PathScore(
            path=path,
            total_score=total_score,
            length_score=length_score,
            starvation_score=starvation_penalty,
            backtracking_penalty=backtracking_penalty,
            dead_end_penalty=dead_end_penalty
        )

    def _calculate_backtracking_penalty(
        self,
        path: List[str],
        previous_location: Optional[Location]
    ) -> float:
        """Calculate penalty for immediate backtracking (A->B->A pattern)."""
        if previous_location is None or len(path) < 2:
            return 0.0

        # Check if first hop goes back to previous location
        if path[1] == previous_location.name:
            return self._weights.backtracking_penalty

        return 0.0

    def _calculate_dead_end_penalty(
        self,
        path: List[str],
        original_target: Optional[Location]
    ) -> float:
        """
        Calculate penalty for dead-end parking pads during deadlock resolution.

        NOTE: For deadlock resolution, we're just trying to get out of the way,
        NOT trying to find a path to the original target. So this penalty should
        not be applied. It's here for potential future use with different deadlock
        resolution strategies.
        """
        # DISABLED: During deadlock, goal is to get out of the way, not reach target
        # Dead-end detection was causing threads to prefer nearby dead-ends over
        # farther parking pads that would actually clear the deadlock
        return 0.0

        # Original logic (kept for reference):
        # if original_target is None or len(path) < 2:
        #     return 0.0
        # parking_pad = path[-1]
        # source = path[0]
        # try:
        #     paths_to_target = self._system_map.get_all_shortest_any_paths(
        #         parking_pad, original_target.teachpoint_name
        #     )
        #     has_non_backtracking_path = any(source not in p[1:] for p in paths_to_target)
        #     if not has_non_backtracking_path:
        #         return self._weights.dead_end_penalty
        # except Exception:
        #     return self._weights.dead_end_penalty * 0.5
        # return 0.0

    def select_best_path(
        self,
        paths: List[List[str]],
        thread_id: str,
        starvation_score: int,
        previous_location: Optional[Location] = None,
        original_target: Optional[Location] = None,
        blocked_location: Optional[Location] = None,
    ) -> Tuple[List[str], PathScore]:
        """
        Score all paths and return the best one.

        Returns:
            Tuple of (best_path, best_score)
        """
        if not paths:
            raise ValueError("Cannot select best path from empty list")

        scored = self.score_paths(
            paths,
            thread_id,
            starvation_score,
            previous_location,
            original_target,
            blocked_location
        )

        best = scored[0]

        # Log the selection for debugging
        orca_logger.debug(
            f"Thread {thread_id} - Selected path: {' -> '.join(best.path)} "
            f"(score: {best.total_score:.2f})"
        )

        return best.path, best
