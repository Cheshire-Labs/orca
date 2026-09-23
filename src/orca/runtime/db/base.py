"""Declarative base + UTC helper for orca-core's persistence layer.

Engine-agnostic: the same metadata backs SQLite (source-available default) and
Postgres (a hosted deployment). Models register on this Base so Alembic autogenerate and
``Base.metadata.create_all`` see one schema regardless of dialect.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    """Current UTC time as a naive datetime for cross-dialect storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
