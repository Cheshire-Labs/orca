"""The event store's reads and writes, for any database. A row's id is the event's cursor."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orca.events.runtime_event import RuntimeEvent
from orca.runtime.db.models import RuntimeEventRow
from orca.runtime.event_store import EventQuery, StoredEvent


async def append(session: AsyncSession, event: RuntimeEvent) -> None:
    session.add(RuntimeEventRow(
        event_name=event.event_name,
        execution_id=event.execution_id or None,
        entity_type=event.entity_type or None,
        entity_id=event.entity_id or None,
        status=event.status or None,
        timestamp=event.timestamp,
        details=event.to_dict(),
    ))
    await session.commit()


async def read(session: AsyncSession, query: EventQuery) -> list[StoredEvent]:
    stmt = select(RuntimeEventRow).where(RuntimeEventRow.id > query.since)
    if query.execution_id is not None:
        stmt = stmt.where(RuntimeEventRow.execution_id == query.execution_id)
    order = RuntimeEventRow.id.desc() if query.tail else RuntimeEventRow.id.asc()
    rows = list((await session.execute(stmt.order_by(order).limit(query.limit))).scalars().all())
    if query.tail:
        rows.reverse()
    return [
        StoredEvent(
            cursor=row.id,
            event_name=row.event_name,
            execution_id=row.execution_id,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            timestamp=row.timestamp,
            details=row.details,
        )
        for row in rows
    ]
