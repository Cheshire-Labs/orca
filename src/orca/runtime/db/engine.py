"""Async engine + session-factory helpers.

``create_memory_engine`` (shared in-memory SQLite) is what the runtime default
and the daemon build today; ``create_sqlite_engine`` is the file-backed engine
used by durability tests and adopted when a deployment configures a DB path.
``make_session_factory`` is engine-agnostic, so a Postgres engine (a hosted deployment)
reuses it unchanged.
"""

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool

from orca.runtime.db.base import Base

# Blocked-writer wait before 'database is locked'. Absorbs a runtime write
# burst; a long-lived file-backed daemon otherwise stalls readers under load.
_SQLITE_BUSY_TIMEOUT_MS = 30_000


def apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Put a SQLite engine in WAL mode with a busy_timeout on every connection.

    WAL lets readers proceed while a writer holds the lock; busy_timeout bounds
    how long a blocked writer waits before raising 'database is locked'. Shared
    so any caller that builds its own SQLite engine hardens it identically
    without duplicating the pragma logic.
    """

    @event.listens_for(engine.sync_engine, "connect")
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


async def create_all_tables(engine: AsyncEngine) -> None:
    """Create every registered table on the engine (in-memory/sim schema setup;
    file-backed deployments use Alembic instead)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
