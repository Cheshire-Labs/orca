"""Deadlock recovery strategy for preventing livelock during deadlock resolution.

When two threads deadlock and the resolver sends them to parking pads,
they can endlessly bounce between the same pads (livelock). This module
provides two mechanisms to break the oscillation:

1. Occupied-pad penalty: pads reserved by other threads are penalized
   in the path scorer, breaking the symmetry where both threads pick
   the same destination.

2. Per-thread cooldown: pads a thread has already visited during the
   current deadlock resolution episode are penalized, preventing the
   A->B->A oscillation pattern. Resets when the thread successfully
   completes a non-deadlock move.
"""


class DeadlockRecoveryStrategy:
    """Tracks deadlock resolution state and provides avoidance sets for path scoring."""

    def __init__(self) -> None:
        self._visited: dict[str, set[str]] = {}

    def record_visit(self, thread_id: str, pad_name: str) -> None:
        """Record that a thread visited a parking pad during deadlock resolution."""
        if thread_id not in self._visited:
            self._visited[thread_id] = set()
        self._visited[thread_id].add(pad_name)

    def get_cooldown(self, thread_id: str) -> set[str]:
        """Return the set of pads this thread has visited during the current episode."""
        return self._visited.get(thread_id, set()).copy()

    def clear_cooldown(self, thread_id: str) -> None:
        """Reset cooldown for a thread after it escapes deadlock resolution."""
        self._visited.pop(thread_id, None)

    def get_locations_to_avoid(
        self, thread_id: str, reserved_by_others: set[str]
    ) -> set[str]:
        """Combine reserved-by-others with this thread's cooldown set."""
        return reserved_by_others | self.get_cooldown(thread_id)
