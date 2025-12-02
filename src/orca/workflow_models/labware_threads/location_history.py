"""
Location history tracking for labware threads to detect and prevent movement loops.
"""
from typing import TYPE_CHECKING, List, Optional
import logging

if TYPE_CHECKING:
    from orca.resource_models.location import Location

orca_logger = logging.getLogger("orca")


class LocationHistory:
    """
    Tracks the complete location history of a labware thread to detect oscillation patterns
    and prevent backtracking during deadlock resolution.

    Maintains unbounded history for complete labware movement tracking, allowing for
    future visualization and analysis capabilities. Stores Location objects (not just names)
    to preserve full context for future analysis.

    This class serves as the single source of truth for a thread's current and historical locations.
    """

    def __init__(self) -> None:
        """Initialize location history tracker with unbounded history."""
        self._history: List['Location'] = []

    def add_location(self, location: 'Location') -> None:
        """
        Add a location to the history.

        Args:
            location: Location object to add to history
        """
        self._history.append(location)

    def get_previous_location(self) -> Optional['Location']:
        """
        Get the previous location (second most recent).

        Returns:
            Previous Location object, or None if not enough history
        """
        if len(self._history) < 2:
            return None
        return self._history[-2]

    def get_current_location(self) -> Optional['Location']:
        """
        Get the current location (most recent).

        This serves as the single source of truth for the thread's current location.

        Returns:
            Current Location object, or None if history is empty
        """
        if len(self._history) == 0:
            return None
        return self._history[-1]

    def get_history(self) -> List['Location']:
        """
        Get the full location history.

        Returns:
            List of Location objects in chronological order (oldest to newest)
        """
        return list(self._history)

    def get_history_names(self) -> List[str]:
        """
        Get the location names from history.

        Returns:
            List of location names in chronological order (oldest to newest)
        """
        return [loc.name for loc in self._history]

    def would_backtrack(self, target_location: 'Location') -> bool:
        """
        Check if moving to the target location would be backtracking to the previous location.

        Args:
            target_location: The location being considered for movement

        Returns:
            True if this would be immediate backtracking (A→B→A pattern)
        """
        previous = self.get_previous_location()
        if previous is None:
            return False
        return target_location.name == previous.name

    def detect_oscillation(self, min_pattern_length: int = 2) -> Optional[List[str]]:
        """
        Detect if the thread is oscillating between a small set of locations.

        Checks for patterns like:
        - A→B→A→B (2-location oscillation)
        - A→B→C→A→B→C (3-location oscillation)

        Args:
            min_pattern_length: Minimum pattern length to detect (default: 2)

        Returns:
            The repeating pattern (location names) if detected, None otherwise
        """
        if len(self._history) < min_pattern_length * 2:
            return None

        # Check for 2-location oscillation (A→B→A→B)
        if min_pattern_length == 2 and len(self._history) >= 4:
            if (self._history[-1].name == self._history[-3].name and
                self._history[-2].name == self._history[-4].name):
                return [self._history[-2].name, self._history[-1].name]

        # Check for 3-location oscillation (A→B→C→A→B→C)
        if min_pattern_length <= 3 and len(self._history) >= 6:
            pattern_names = [self._history[-3].name, self._history[-2].name, self._history[-1].name]
            if (self._history[-3].name == self._history[-6].name and
                self._history[-2].name == self._history[-5].name and
                self._history[-1].name == self._history[-4].name):
                return pattern_names

        return None

    def clear(self) -> None:
        """Clear all location history."""
        self._history.clear()

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"LocationHistory({' → '.join(str(loc) for loc in self._history)})"

    def __len__(self) -> int:
        """Return the number of locations in history."""
        return len(self._history)
