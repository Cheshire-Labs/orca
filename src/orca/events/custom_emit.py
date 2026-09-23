"""Bridge ctx.emit publishes onto the workflow event bus as CUSTOM.* events.

The EventChannelRegistry a ctx.emit publishes to is in-process thread
coordination (what ctx.wait_for reads); without this bridge the author's own
domain signals never reach the RuntimeEvent pipeline, so no sink, archive, or
events API ever sees them.
"""

from typing import Callable

from pydantic import JsonValue

from orca.events.execution_context import CustomEventContext, ExecutionContext

EventEmitter = Callable[[str, ExecutionContext], None]


def emit_custom_event(
    emitter: EventEmitter | None,
    *,
    execution_id: str,
    workflow_name: str | None,
    thread_id: str | None,
    event_name: str,
    value: str | None,
    data: dict[str, JsonValue] | None,
) -> None:
    """Best-effort: a directly-built context (no emitter wired) keeps working
    off the EventChannel path alone. Dots in the author's name are sanitized
    for the bus's three-part event grammar; the context keeps the name verbatim.
    """
    if emitter is None or workflow_name is None:
        return
    safe_name = event_name.replace(".", "_")
    emitter(
        f"CUSTOM.{safe_name}.EMITTED",
        CustomEventContext(
            execution_id=execution_id,
            workflow_name=workflow_name,
            thread_id=thread_id,
            event_name=event_name,
            value=value,
            data=data or {},
        ),
    )
