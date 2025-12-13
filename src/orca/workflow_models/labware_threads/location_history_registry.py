"""
Registry for tracking location history across all labware threads.
"""
from typing import Dict
from orca.workflow_models.labware_threads.location_history import LocationHistory


class LabwareLocationHistoryRegistry:
    """
    Centralized registry for tracking location history of all labware threads.

    This allows:
    - Centralized access to movement history for all labware
    - Future visualization and analysis capabilities
    - Separation of concerns (history tracking separate from thread execution)
    """

    def __init__(self) -> None:
        """Initialize the history registry."""
        self._histories: Dict[str, LocationHistory] = {}

    def get_or_create_history(self, labware_id: str) -> LocationHistory:
        """
        Get the location history for a labware thread, creating if it doesn't exist.

        Args:
            labware_id: Unique identifier for the labware thread

        Returns:
            LocationHistory instance for the labware
        """
        if labware_id not in self._histories:
            self._histories[labware_id] = LocationHistory()
        return self._histories[labware_id]

    def get_history(self, labware_id: str) -> LocationHistory:
        """
        Get the location history for a labware thread.

        Args:
            labware_id: Unique identifier for the labware thread

        Returns:
            LocationHistory instance for the labware

        Raises:
            KeyError: If no history exists for this labware
        """
        if labware_id not in self._histories:
            raise KeyError(f"No location history found for labware {labware_id}")
        return self._histories[labware_id]

    def has_history(self, labware_id: str) -> bool:
        """
        Check if history exists for a labware thread.

        Args:
            labware_id: Unique identifier for the labware thread

        Returns:
            True if history exists, False otherwise
        """
        return labware_id in self._histories

    def clear_history(self, labware_id: str) -> None:
        """
        Clear the history for a specific labware thread.

        Args:
            labware_id: Unique identifier for the labware thread
        """
        if labware_id in self._histories:
            self._histories[labware_id].clear()

    def clear_all(self) -> None:
        """Clear all labware histories."""
        self._histories.clear()
