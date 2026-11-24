"""Activity stores in a temporary SQLite file, for a test."""

import tempfile
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from orca.runtime.db import Base, apply_sqlite_pragmas, create_blocking_sqlite_engine
from orca.runtime.sqlite_event_store import SqliteEventStore
from orca.runtime.sqlite_ops_history_store import SqliteOpsHistoryStore

OPS_HISTORY_TABLES = ("ops_history", "ops_history_labware", "ops_history_device")


def activity_engine(path: Path, tables: tuple[str, ...]) -> AsyncEngine:
    """An engine over `path` with `tables` created.

    No connection outlives a session, so nothing is left open when a test's event loop ends.
    """
    schema = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(schema, tables=[Base.metadata.tables[name] for name in tables])
    schema.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", poolclass=NullPool)
    apply_sqlite_pragmas(engine)
    return engine


def ops_history_engine(path: Path) -> Engine:
    engine = create_blocking_sqlite_engine(path)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in OPS_HISTORY_TABLES])
    return engine


class EphemeralEventStore(SqliteEventStore):
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orca_events_", ignore_cleanup_errors=True)
        super().__init__(activity_engine(Path(self._tmp.name) / "events.db", ("runtime_events",)))


class EphemeralOpsHistoryStore(SqliteOpsHistoryStore):
    def __init__(self) -> None:
        # Held so the folder lives as long as the store.
        self._tmp = tempfile.TemporaryDirectory(prefix="orca_ops_history_", ignore_cleanup_errors=True)
        super().__init__(ops_history_engine(Path(self._tmp.name) / "ops.db"))
