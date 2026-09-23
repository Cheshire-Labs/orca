"""LabwareJourneyTracker plugin: per-thread location history tracking.

Tracks where each labware thread has been, is now, and will go.
Queryable live during execution -- reads directly from the thread's
LocationHistory via the system, no data duplication. State is keyed
by thread_id for safe concurrent-workflow tracking.

Usage:
    tracker = LabwareJourneyTracker()
    runtime.register_plugin(tracker)

    # Mid-run (by thread name or id prefix):
    tracker.get_live_journey("abc1...")      # ["stacker_3", "bravo_96", "shaker_1"]
    tracker.get_current_location("abc1...")  # "shaker_1"

    # Post-run:
    tracker.get_completed_journey("abc1...")  # ["stacker_3", ..., "waste_1"]
"""

from typing import Dict, List, Optional

from orca.events.execution_context import ThreadExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin, PluginCommand
from orca.runtime.entity_resolver import resolve_entity


class LabwareJourneyTracker(OrcaPlugin):
    """Per-thread location journey tracking via live system queries."""

    def __init__(self) -> None:
        self._thread_names: Dict[str, str] = {}  # thread_id -> thread_name
        self._completed_journeys: Dict[str, List[str]] = {}  # thread_id -> location names

    def get_commands(self) -> list[PluginCommand]:
        return [
            PluginCommand(
                name="journey",
                description="Show location history for a thread",
                usage="journey <thread_name_or_id>",
                handler=self._cmd_journey,
            ),
        ]

    async def _cmd_journey(self, args: list[str]) -> str:
        if not args:
            return "Usage: journey <thread_name_or_id>"
        try:
            thread_id = resolve_entity(args[0], self._thread_names)
        except ValueError as e:
            return str(e)
        if thread_id is None:
            return f"Thread '{args[0]}' not found."
        thread_name = self._thread_names[thread_id]
        live = self.get_live_journey(thread_id)
        current = self.get_current_location(thread_id)
        completed = self.get_completed_journey(thread_id)

        lines: list[str] = [f"Thread: {thread_name} ({thread_id[:8]})"]
        if current:
            lines.append(f"  Current location: {current}")
        else:
            lines.append("  Current location: (none)")
        if live:
            lines.append(f"  Journey: {' -> '.join(live)}")
        if completed:
            lines.append(f"  Completed journey: {' -> '.join(completed)}")
        return "\n".join(lines)

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        if not isinstance(event.context, ThreadExecutionContext):
            return
        ctx = event.context
        if "CREATED" in event.event_name:
            self._thread_names[ctx.thread_id] = ctx.thread_name
        elif "COMPLETED" in event.event_name:
            thread = self.system.get_executing_thread(ctx.thread_id)
            self._completed_journeys[ctx.thread_id] = thread.location_history_names

    def get_live_journey(self, thread_id: str) -> List[str]:
        """Live location history for this thread."""
        thread = self.system.get_executing_thread(thread_id)
        return thread.location_history_names

    def get_current_location(self, thread_id: str) -> Optional[str]:
        """Current location of this thread."""
        thread = self.system.get_executing_thread(thread_id)
        names = thread.location_history_names
        if names:
            return names[-1]
        return None

    def get_completed_journey(self, thread_id: str) -> List[str]:
        """Snapshot of location journey taken when this thread finished."""
        return self._completed_journeys.get(thread_id, [])

    @property
    def thread_names(self) -> Dict[str, str]:
        """Thread ID to display name mapping."""
        return dict(self._thread_names)

    @property
    def all_completed_journeys(self) -> Dict[str, List[str]]:
        """All thread journey snapshots keyed by thread_id."""
        return dict(self._completed_journeys)
