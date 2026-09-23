"""RuntimeEvent: structured, JSON-serializable event from the runtime.

Wraps per-workflow EventBus events with execution_id and timestamp
for system-level consumption by plugins, sinks, and external systems.
"""

import time
from typing import Any

from pydantic import BaseModel, ConfigDict
from typing_extensions import Self

from orca.events.execution_context import ExecutionContext


class RuntimeEvent(BaseModel):
    """A structured event emitted by the system-level event bus.

    Built from per-workflow EventBus events by the _SystemEventForwarder.
    Plugins access context fields directly (e.g., event.context.thread_id).
    Serialized to dict/JSON via to_dict() for sinks and external systems.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_name: str
    execution_id: str
    timestamp: float
    entity_type: str
    entity_id: str
    status: str
    context: ExecutionContext

    @classmethod
    def from_event_bus(
        cls,
        event_name: str,
        execution_id: str,
        context: ExecutionContext,
    ) -> Self:
        parts = event_name.split(".")
        if len(parts) == 3:
            entity_type, entity_id, status = parts
        elif len(parts) == 2:
            entity_type, status = parts
            entity_id = ""
        else:
            entity_type, entity_id, status = event_name, "", ""

        return cls(
            event_name=event_name,
            execution_id=execution_id,
            timestamp=time.time(),
            entity_type=entity_type,
            entity_id=entity_id,
            status=status,
            context=context,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
