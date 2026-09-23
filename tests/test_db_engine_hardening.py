"""create_sqlite_engine must configure file-backed SQLite for concurrent access.

WAL lets readers proceed while a writer holds the lock; busy_timeout bounds how
long a blocked writer waits before raising 'database is locked'. Without them one
writer blocks every concurrent reader -- the lock storm a long-lived file-backed
daemon hits under load. The in-memory StaticPool engine is exempt (single shared
connection, so no cross-connection lock contention exists).
"""

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orca.runtime.db import apply_sqlite_pragmas, create_sqlite_engine


async def test_file_engine_enables_wal_and_busy_timeout(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "harden.db")
    try:
        async with engine.connect() as conn:
            journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        assert journal_mode == "wal"
        assert busy_timeout == 30000
    finally:
        await engine.dispose()


async def test_open_writer_does_not_lock_out_reader(tmp_path: Path) -> None:
    """A held-open write must not raise 'database is locked' on a concurrent read.

    Under WAL the reader sees the last committed snapshot instead of blocking --
    the exact failure a file-backed daemon hits with a write in flight."""
    engine = create_sqlite_engine(tmp_path / "harden.db")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
            await conn.execute(text("INSERT INTO t (v) VALUES ('seed')"))

        async with engine.connect() as writer, engine.connect() as reader:
            await writer.execute(text("INSERT INTO t (v) VALUES ('pending')"))
            count = (await reader.execute(text("SELECT count(*) FROM t"))).scalar()
            assert count == 1  # committed snapshot only, and crucially no lock error
            await writer.commit()
    finally:
        await engine.dispose()


async def test_apply_sqlite_pragmas_hardens_an_arbitrary_engine(tmp_path: Path) -> None:
    """The shared helper hardens any SQLite engine -- the contract a caller that
    builds its own engine relies on to harden it without duplicating pragmas."""
    db = (tmp_path / "helper.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    apply_sqlite_pragmas(engine)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 30000
    finally:
        await engine.dispose()
