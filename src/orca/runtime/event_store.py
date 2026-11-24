"""The stored runtime event, and the store interface every server writes events to.

`LogSink` logs each event to the `orca.events` logger with the event attached as
`extra={EVENT_ATTR: event}`. A handler reads it back with `event_of`.
"""

import logging
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from orca.events.execution_context import HostedEventContext
from orca.events.runtime_event import RuntimeEvent

EVENT_LOGGER_NAME = "orca.events"
EVENT_ATTR = "runtime_event"

def is_stored(event: RuntimeEvent) -> bool:
    """Every event orca-core sends is stored. A hosted server's own events are its to keep."""
    return not isinstance(event.context, HostedEventContext)


class StoredEvent(BaseModel):
    """One stored event. `cursor` orders events in the order they were written."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cursor: int
    event_name: str
    execution_id: str | None
    entity_type: str | None
    entity_id: str | None
    timestamp: float
    details: dict[str, JsonValue]


class EventQuery(BaseModel):
    """Events after `since`, oldest first; with `tail`, the newest `limit` of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    since: int = 0
    execution_id: str | None = None
    limit: int = 500
    tail: bool = False


class IEventStore(Protocol):
    """Dumb per-database persistence for runtime events."""

    async def append(self, event: RuntimeEvent) -> None: ...

    async def read(self, query: EventQuery) -> list[StoredEvent]: ...


def not_a_runtime_event(record: logging.LogRecord) -> bool:
    """A filter for a diagnostic log handler: events are stored, and would flood a log file."""
    return record.name != EVENT_LOGGER_NAME


def event_of(record: logging.LogRecord) -> RuntimeEvent | None:
    event = getattr(record, EVENT_ATTR, None)
    return event if isinstance(event, RuntimeEvent) else None
