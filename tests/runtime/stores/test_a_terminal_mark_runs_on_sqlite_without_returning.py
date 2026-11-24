"""Marking a run terminal sends no RETURNING, which SQLite before 3.35 cannot run (Debian 11 ships 3.34)."""

from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.engine.interfaces import DBAPICursor

from orca.runtime.db import create_all_tables, create_memory_engine
from orca.runtime.execution_record import ExecutionState
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore


async def test_marking_a_run_terminal_sends_no_returning() -> None:
    engine = create_memory_engine()
    await create_all_tables(engine)
    store = SqliteExecutionRecordStore(engine)
    await store.upsert_running("exec-1", "assay", datetime(2026, 3, 1, tzinfo=timezone.utc))
    sent: list[str] = []

    def _record(
        _conn: Connection, _cursor: DBAPICursor, statement: str, _params: tuple[str, ...],
        _context: ExecutionContext, _many: bool,
    ) -> None:
        sent.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        assert await store.mark_terminal("exec-1", ExecutionState.COMPLETED, None, datetime.now(timezone.utc))
        assert await store.mark_terminal("unknown", ExecutionState.FAILED, None, datetime.now(timezone.utc)) is False
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)
        await engine.dispose()

    assert sent, "the listener saw no SQL"
    assert [s for s in sent if "RETURNING" in s.upper()] == []
