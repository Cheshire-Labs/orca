"""Declarative base + UTC helper for orca-core's persistence layer.

Engine-agnostic: the same metadata backs SQLite (source-available default) and
a hosted deployment's database. Models register on this Base so Alembic autogenerate and
``Base.metadata.create_all`` see one schema regardless of dialect.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    """Current UTC time as a naive datetime for cross-dialect storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive_utc(value: datetime) -> datetime:
    """``value`` as the naive UTC a column stamped by ``utc_now`` holds.

    Some drivers refuse an aware value in a column with no time zone."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def epoch_seconds(value: datetime) -> float:
    """Seconds since the epoch for a column stamped by ``utc_now``.

    The column is naive and holds UTC; ``datetime.timestamp()`` would read it as
    local time and shift it by the machine's UTC offset."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).timestamp()
    return value.timestamp()
