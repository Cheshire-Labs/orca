"""The audit store's reads and writes, for any database."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orca.runtime.audit_store import AuditEntry, AuditQuery
from orca.runtime.db.models import AuditEntryRow


async def append(session: AsyncSession, entry: AuditEntry) -> None:
    session.add(AuditEntryRow(
        timestamp=entry.timestamp,
        action_name=entry.action_name,
        danger_level=entry.danger_level,
        reason=entry.reason,
        call_args=entry.call_args,
    ))
    await session.commit()


async def newest_first(session: AsyncSession, query: AuditQuery) -> list[AuditEntry]:
    stmt = select(AuditEntryRow)
    if query.action_name is not None:
        stmt = stmt.where(AuditEntryRow.action_name == query.action_name)
    if query.danger_level is not None:
        stmt = stmt.where(AuditEntryRow.danger_level == query.danger_level)
    if query.since is not None:
        stmt = stmt.where(AuditEntryRow.timestamp >= query.since)
    if query.until is not None:
        stmt = stmt.where(AuditEntryRow.timestamp <= query.until)
    stmt = stmt.order_by(AuditEntryRow.id.desc()).limit(query.limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        AuditEntry(
            timestamp=row.timestamp,
            action_name=row.action_name,
            danger_level=row.danger_level,
            reason=row.reason,
            call_args=row.call_args,
        )
        for row in rows
    ]
