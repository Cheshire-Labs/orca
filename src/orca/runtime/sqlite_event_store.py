from sqlalchemy.ext.asyncio import AsyncEngine

from orca.events.runtime_event import RuntimeEvent
from orca.runtime.db import event_queries, make_session_factory
from orca.runtime.event_store import EventQuery, IEventStore, StoredEvent


class SqliteEventStore(IEventStore):
    """Runtime events in the activity database. The server creates the table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sf = make_session_factory(engine)

    async def append(self, event: RuntimeEvent) -> None:
        async with self._sf() as session:
            await event_queries.append(session, event)

    async def read(self, query: EventQuery) -> list[StoredEvent]:
        async with self._sf() as session:
            return await event_queries.read(session, query)
