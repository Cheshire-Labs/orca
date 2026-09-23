"""_SystemEventForwarder: bridges per-workflow EventBus to SystemEventBus.

Subscribed as a global handler on the shared EventBus via subscribe_all().
Receives every per-workflow event and emits it on the SystemEventBus as a
RuntimeEvent tagged with the originating execution_id (which is the same
value as the workflow instance's id; see WorkflowInstance.__init__).

`register_execution` / `unregister_execution` track which executions the
runtime knows about so stale / orphan events from old workflow instances
don't leak into the system bus after shutdown.
"""

from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import ExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.system_event_bus import SystemEventBus


class _SystemEventForwarder(IEventHandler):

    def __init__(self, system_bus: SystemEventBus) -> None:
        self._system_bus = system_bus
        self._known_executions: set[str] = set()

    def register_execution(self, execution_id: str) -> None:
        self._known_executions.add(execution_id)

    def unregister_execution(self, execution_id: str) -> None:
        self._known_executions.discard(execution_id)

    def handle(self, event: str, context: ExecutionContext) -> None:
        if context.execution_id not in self._known_executions:
            return

        runtime_event = RuntimeEvent.from_event_bus(
            event_name=event,
            execution_id=context.execution_id,
            context=context,
        )
        self._system_bus.emit(runtime_event)
