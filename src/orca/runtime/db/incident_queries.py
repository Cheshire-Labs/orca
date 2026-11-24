"""The incident store's reads and writes, for any database."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orca.runtime.db.incident_mapping import incident_to_row, row_to_incident
from orca.runtime.db.models import IncidentRow
from orca.runtime.incident_store import IncidentCategory, SystemIncident


async def insert(session: AsyncSession, incident: SystemIncident) -> None:
    session.add(incident_to_row(incident))
    await session.commit()


async def get(session: AsyncSession, incident_id: str) -> SystemIncident | None:
    row = await session.get(IncidentRow, incident_id)
    return None if row is None else row_to_incident(row)


async def fetch(
    session: AsyncSession,
    *,
    unacknowledged_only: bool,
    category: IncidentCategory | None,
    execution_id: str | None,
    since: float | None,
) -> list[SystemIncident]:
    """Incidents matching every filter given, oldest first."""
    stmt = select(IncidentRow)
    if unacknowledged_only:
        stmt = stmt.where(IncidentRow.acknowledged.is_(False))
    if category is not None:
        stmt = stmt.where(IncidentRow.category == category.name)
    if execution_id is not None:
        stmt = stmt.where(IncidentRow.execution_id == execution_id)
    if since is not None:
        stmt = stmt.where(IncidentRow.timestamp >= since)
    rows = (await session.execute(stmt.order_by(IncidentRow.timestamp))).scalars().all()
    return [row_to_incident(row) for row in rows]


async def mark_acked(session: AsyncSession, incident_id: str, acknowledged_at: datetime) -> None:
    row = await session.get(IncidentRow, incident_id)
    if row is not None:
        row.acknowledged = True
        row.acknowledged_at = acknowledged_at
        await session.commit()


async def mark_all_acked(
    session: AsyncSession, category: IncidentCategory | None, acknowledged_at: datetime,
) -> int:
    """Acknowledge every unacknowledged incident, of one category when given. Returns how many."""
    stmt = select(IncidentRow).where(IncidentRow.acknowledged.is_(False))
    if category is not None:
        stmt = stmt.where(IncidentRow.category == category.name)
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        row.acknowledged = True
        row.acknowledged_at = acknowledged_at
    await session.commit()
    return len(rows)
