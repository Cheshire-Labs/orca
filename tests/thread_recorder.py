"""A test plugin that records each thread's name, and its methods and locations when it completes."""

from orca.events.execution_context import ThreadExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin


class ThreadRecorder(OrcaPlugin):
    def __init__(self) -> None:
        self.thread_names: dict[str, str] = {}
        self.completed_methods: dict[str, list[str]] = {}
        self.completed_journeys: dict[str, list[str]] = {}

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        if not isinstance(event.context, ThreadExecutionContext):
            return
        ctx = event.context
        if "CREATED" in event.event_name:
            self.thread_names[ctx.thread_id] = ctx.thread_name
        elif "COMPLETED" in event.event_name:
            thread = self.system.get_executing_thread(ctx.thread_id)
            self.completed_methods[ctx.thread_id] = [m.name for m in thread.completed_methods]
            self.completed_journeys[ctx.thread_id] = thread.location_history_names
