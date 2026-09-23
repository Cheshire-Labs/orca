"""MethodTracker plugin: per-thread method execution tracking.

Tracks which methods each labware thread has completed, is currently
executing, and has pending. Queryable live during execution. State is
keyed by thread_id for safe concurrent-workflow tracking.

Usage:
    tracker = MethodTracker()
    runtime.register_plugin(tracker)

    # Mid-run (by thread name or id prefix):
    tracker.get_current_method("abc1...")       # "post_capture_wash"
    tracker.get_completed_methods("abc1...")     # ["delid", "sample_to_bead_plate", ...]
    tracker.get_completed_methods("abc1...")     # ["delid", "sample_to_bead_plate", ...]

    # Post-run:
    tracker.get_completed_snapshot("abc1...")    # final method list
"""

from typing import Dict, List, Optional

from orca.events.execution_context import ThreadExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin, PluginCommand
from orca.runtime.entity_resolver import resolve_entity


class MethodTracker(OrcaPlugin):
    """Per-thread method lifecycle tracking via live system queries."""

    def __init__(self) -> None:
        self._thread_names: Dict[str, str] = {}  # thread_id -> thread_name
        self._completed_snapshots: Dict[str, List[str]] = {}  # thread_id -> method names

    def get_commands(self) -> list[PluginCommand]:
        return [
            PluginCommand(
                name="tracker",
                description="Show method execution progress for a thread",
                usage="tracker <thread_name_or_id>",
                handler=self._cmd_tracker,
            ),
        ]

    async def _cmd_tracker(self, args: list[str]) -> str:
        if not args:
            return "Usage: tracker <thread_name_or_id>"
        try:
            thread_id = resolve_entity(args[0], self._thread_names)
        except ValueError as e:
            return str(e)
        if thread_id is None:
            return f"Thread '{args[0]}' not found."
        thread_name = self._thread_names[thread_id]
        current = self.get_current_method(thread_id)
        completed = self.get_completed_methods(thread_id)

        lines: list[str] = [f"Thread: {thread_name} ({thread_id[:8]})"]
        if current:
            lines.append(f"  Current method: {current}")
        else:
            lines.append("  Current method: (none)")
        if completed:
            lines.append(f"  Completed: {', '.join(completed)}")
        return "\n".join(lines)

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        if not isinstance(event.context, ThreadExecutionContext):
            return
        ctx = event.context
        if "CREATED" in event.event_name:
            self._thread_names[ctx.thread_id] = ctx.thread_name
        elif "COMPLETED" in event.event_name:
            thread = self.system.get_executing_thread(ctx.thread_id)
            self._completed_snapshots[ctx.thread_id] = [m.name for m in thread.completed_methods]

    def get_current_method(self, thread_id: str) -> Optional[str]:
        thread = self.system.get_executing_thread(thread_id)
        if thread.assigned_method is not None:
            return thread.assigned_method.name
        return None

    def get_completed_methods(self, thread_id: str) -> List[str]:
        thread = self.system.get_executing_thread(thread_id)
        return [m.name for m in thread.completed_methods]

    def get_completed_snapshot(self, thread_id: str) -> List[str]:
        """Snapshot of completed methods taken when this thread finished."""
        return self._completed_snapshots.get(thread_id, [])

    @property
    def thread_names(self) -> Dict[str, str]:
        """Thread ID to display name mapping."""
        return dict(self._thread_names)

    @property
    def all_completed_snapshots(self) -> Dict[str, List[str]]:
        """All thread completion snapshots keyed by thread_id."""
        return dict(self._completed_snapshots)
