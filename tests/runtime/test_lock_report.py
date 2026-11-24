"""A write that fails with 'database is locked' logs who in this process holds the lock."""

import asyncio
import gc
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_blocking_sqlite_engine, create_sqlite_engine
from orca.runtime.db import lock_report

_INSERT = text("INSERT INTO t (v) VALUES (1)")


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v INTEGER)")
    con.commit()
    con.close()
    yield path


async def _hold_the_write_lock(engine: AsyncEngine, held: asyncio.Event, release: asyncio.Event) -> None:
    async with engine.connect() as conn:
        await conn.execute(_INSERT)
        held.set()
        await release.wait()
        await conn.commit()


def _hold_the_write_lock_in_a_thread(engine: Engine, held: threading.Event, release: threading.Event) -> None:
    with engine.connect() as conn:
        conn.execute(_INSERT)
        held.set()
        release.wait()
        conn.commit()


async def _write_and_get_locked_out(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA busy_timeout=100")
        with pytest.raises(OperationalError, match="database is locked"):
            await conn.execute(_INSERT)


def _write_and_get_locked_out_blocking(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=100")
        with pytest.raises(OperationalError, match="database is locked"):
            conn.execute(_INSERT)


def _report_text(caplog: pytest.LogCaptureFixture) -> str:
    reports = [r.getMessage() for r in caplog.records if r.name == lock_report.__name__]
    assert len(reports) == 1
    return reports[0]


async def test_a_locked_out_write_names_the_task_holding_the_lock_and_its_stack(
    db_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    holder_engine = create_sqlite_engine(db_path)
    victim_engine = create_sqlite_engine(db_path)
    held, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_the_write_lock(holder_engine, held, release), name="holder-task")
    await held.wait()

    with caplog.at_level("ERROR", logger=lock_report.__name__):
        await _write_and_get_locked_out(victim_engine)

    release.set()
    await holder
    report = _report_text(caplog)
    assert "in a transaction and idle (likely holding the lock), checked out by task 'holder-task'" in report
    assert "_hold_the_write_lock" in report
    assert "- the connection whose statement failed" in report
    assert "No connection in this process is idle" not in report
    await holder_engine.dispose()
    await victim_engine.dispose()


async def test_a_locked_out_write_from_a_worker_thread_names_the_thread_holding_the_lock(
    db_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    holder_engine = create_blocking_sqlite_engine(db_path)
    victim_engine = create_blocking_sqlite_engine(db_path)
    held, release = threading.Event(), threading.Event()
    holder = threading.Thread(
        target=_hold_the_write_lock_in_a_thread, args=(holder_engine, held, release), name="holder-thread",
    )
    holder.start()
    held.wait()

    with caplog.at_level("ERROR", logger=lock_report.__name__):
        await asyncio.to_thread(_write_and_get_locked_out_blocking, victim_engine)

    release.set()
    holder.join()
    report = _report_text(caplog)
    assert "in a transaction and idle (likely holding the lock), checked out by thread 'holder-thread'" in report
    assert "_hold_the_write_lock_in_a_thread" in report
    holder_engine.dispose()
    victim_engine.dispose()


async def test_a_lock_held_outside_orca_says_no_connection_in_this_process_holds_it(
    db_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    outsider = sqlite3.connect(db_path, isolation_level=None)
    outsider.execute("BEGIN IMMEDIATE")
    victim_engine = create_sqlite_engine(db_path)

    with caplog.at_level("ERROR", logger=lock_report.__name__):
        await _write_and_get_locked_out(victim_engine)

    outsider.rollback()
    outsider.close()
    assert "No connection in this process is idle in a transaction on this file" in _report_text(caplog)
    await victim_engine.dispose()


async def _wait_for_the_lock(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA busy_timeout=10000")
        await conn.execute(_INSERT)
        await conn.commit()


def _wait_for_the_lock_in_a_thread(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=10000")
        conn.execute(_INSERT)
        conn.commit()


async def _until_in_a_transaction(database: str | None, count: int) -> None:
    deadline = time.monotonic() + 10
    while sum(
        entry.database == database and getattr(record.driver_connection, "in_transaction", False)
        for record, entry in list(lock_report._connections.items())
    ) < count:
        assert time.monotonic() < deadline, f"{count} writers never started waiting"
        await asyncio.sleep(0.01)


async def test_writers_waiting_for_the_lock_are_not_reported_as_holding_it(
    db_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    outsider = sqlite3.connect(db_path, isolation_level=None)
    outsider.execute("BEGIN IMMEDIATE")
    async_engine = create_sqlite_engine(db_path)
    blocking_engine = create_blocking_sqlite_engine(db_path)
    task_waiter = asyncio.create_task(_wait_for_the_lock(async_engine), name="waiting-task")
    thread_waiter = threading.Thread(
        target=_wait_for_the_lock_in_a_thread, args=(blocking_engine,), name="waiting-thread",
    )
    thread_waiter.start()
    await _until_in_a_transaction(async_engine.url.database, count=2)

    with caplog.at_level("ERROR", logger=lock_report.__name__):
        await _write_and_get_locked_out(async_engine)

    outsider.rollback()
    outsider.close()
    await task_waiter
    thread_waiter.join()
    report = _report_text(caplog)
    waiting = "in a transaction and running a statement (likely waiting for the lock)"
    assert f"{waiting}, checked out by task 'waiting-task'" in report
    assert f"{waiting}, checked out by thread 'waiting-thread'" in report
    assert "No connection in this process is idle in a transaction on this file" in report
    await async_engine.dispose()
    blocking_engine.dispose()


def test_an_engine_dropped_without_dispose_leaves_nothing_in_the_report_registry(db_path: Path) -> None:
    engine = create_blocking_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(_INSERT)
    database = engine.url.database
    del engine, conn
    gc.collect()

    assert all(entry.database != database for entry in lock_report._connections.values())


async def test_a_report_that_fails_to_build_still_lets_the_original_error_through(
    db_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken(_database: str, _context: ExceptionContext) -> str:
        raise RuntimeError("report broke")

    monkeypatch.setattr(lock_report, "_report", _broken)
    outsider = sqlite3.connect(db_path, isolation_level=None)
    outsider.execute("BEGIN IMMEDIATE")
    victim_engine = create_sqlite_engine(db_path)

    with caplog.at_level("ERROR", logger=lock_report.__name__):
        await _write_and_get_locked_out(victim_engine)

    outsider.rollback()
    outsider.close()
    assert "Could not build the report" in _report_text(caplog)
    await victim_engine.dispose()
