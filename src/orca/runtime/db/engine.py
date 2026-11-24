"""Async engine + session-factory helpers.

``create_memory_engine`` (shared in-memory SQLite) backs tests;
``create_sqlite_engine`` is the file-backed engine the daemon's activity
database uses.
``make_session_factory`` is engine-agnostic, so a hosted deployment's engine
reuses it unchanged.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Connection, Engine, Table, create_engine, event, inspect
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool

from orca.runtime.db.base import Base
from orca.runtime.db.lock_report import attach_lock_report

# Blocked-writer wait before 'database is locked'. Absorbs a runtime write
# burst; a long-lived file-backed daemon otherwise stalls readers under load.
_SQLITE_BUSY_TIMEOUT_MS = 30_000


def apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Put a SQLite engine in WAL mode with a busy_timeout on every connection.

    WAL lets readers proceed while a writer holds the lock; busy_timeout bounds
    how long a blocked writer waits before raising 'database is locked', and the
    lock report logs who held the lock when that happens. Shared so any caller
    that builds its own SQLite engine hardens it identically.
    """
    _harden(engine.sync_engine)


def _harden(engine: Engine) -> None:
    _apply_pragmas(engine)
    attach_lock_report(engine)


def _apply_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(
        dbapi_conn: DBAPIConnection, _record: ConnectionPoolEntry
    ) -> None:
        cursor = dbapi_conn.cursor()
        # busy_timeout first: the rollback->WAL switch takes a brief exclusive
        # lock, so a concurrent connect waits for it rather than failing BUSY.
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def create_sqlite_engine(db_path: str | Path) -> AsyncEngine:
    """File-backed aiosqlite engine, hardened with WAL + busy_timeout."""
    resolved = Path(db_path).resolve()
    engine = create_async_engine(f"sqlite+aiosqlite:///{resolved.as_posix()}")
    apply_sqlite_pragmas(engine)
    return engine


def create_blocking_sqlite_engine(db_path: str | Path) -> Engine:
    """A synchronous file-backed engine, for a store that runs its queries on worker threads."""
    resolved = Path(db_path).resolve()
    engine = create_engine(
        f"sqlite:///{resolved.as_posix()}", connect_args={"check_same_thread": False},
    )
    _harden(engine)
    return engine


def create_memory_engine() -> AsyncEngine:
    """Shared in-memory SQLite engine for tests and sim.

    StaticPool keeps a single connection so every session (including the
    drain task's executor-thread writes) sees the same in-memory database;
    a plain ``:memory:`` URL would give each connection a private DB. Schema
    is created via ``create_all_tables`` (no Alembic for ephemeral DBs)."""
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory for any AsyncEngine; expire_on_commit off so committed
    rows stay readable without a refresh round-trip."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def create_tables(conn: Connection, tables: Sequence[Table] | None = None) -> None:
    """Create the tables, and any nullable column or index a table made before it was declared lacks."""
    Base.metadata.create_all(conn, tables=tables)
    for table in tables if tables is not None else Base.metadata.sorted_tables:
        _add_missing_nullable_columns(conn, table)
        for index in table.indexes:
            index.create(conn, checkfirst=True)


def _add_missing_nullable_columns(conn: Connection, table: Table) -> None:
    present = {column["name"] for column in inspect(conn).get_columns(table.name)}
    operations = Operations(MigrationContext.configure(conn))
    for column in table.columns:
        if column.name not in present and column.nullable:
            operations.add_column(table.name, Column(column.name, column.type, nullable=True))


async def create_all_tables(engine: AsyncEngine) -> None:
    """Create every registered table on the engine."""
    async with engine.begin() as conn:
        await conn.run_sync(create_tables)
