"""The audit row every server stores, and the store interface it is written to.

Audited actions log to the `orca.audit` logger with the row attached as
`extra={AUDIT_ENTRY_ATTR: entry}`. A handler reads it back with `audit_entry_of`.
"""

import logging
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

AUDIT_LOGGER_NAME = "orca.audit"
AUDIT_ENTRY_ATTR = "audit_entry"


class AuditEntry(BaseModel):
    """One confirmed audited action. `danger_level` is the `DangerLevel` name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: float
    action_name: str
    danger_level: str
    reason: str | None
    call_args: dict[str, JsonValue] = Field(default_factory=dict)


class AuditQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_name: str | None = None
    danger_level: str | None = None
    since: float | None = None
    until: float | None = None
    limit: int = 200


class IAuditStore(Protocol):
    """Dumb per-database persistence for audit rows."""

    async def append(self, entry: AuditEntry) -> None: ...

    async def newest_first(self, query: AuditQuery) -> list[AuditEntry]: ...


def audit_entry_of(record: logging.LogRecord) -> AuditEntry | None:
    entry = getattr(record, AUDIT_ENTRY_ATTR, None)
    return entry if isinstance(entry, AuditEntry) else None
