"""The daemon's audit store keeps every field, filters in SQL, and reads newest first."""

from pathlib import Path

from orca.runtime.audit_store import AuditEntry, AuditQuery
from orca.runtime.db import create_memory_engine, create_sqlite_engine
from orca.runtime.sqlite_audit_store import SqliteAuditStore


def _entry(ts: float, action: str = "teachpoints.add", level: str = "OPERATOR") -> AuditEntry:
    return AuditEntry(
        timestamp=ts, action_name=action, danger_level=level, reason="taught",
        call_args={"teachpoint": {"position_id": "pad_1", "x": 1.5}},
    )


async def test_a_row_reads_back_whole_from_a_new_engine(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = create_sqlite_engine(path)
    await SqliteAuditStore(first).append(_entry(10.0))
    await first.dispose()

    second = create_sqlite_engine(path)
    try:
        [row] = await SqliteAuditStore(second).newest_first(AuditQuery())
    finally:
        await second.dispose()

    assert row == _entry(10.0)


async def test_rows_come_back_in_the_order_they_were_written_newest_first() -> None:
    store = SqliteAuditStore(create_memory_engine())
    for ts in (100.0, 300.0, 200.0):
        await store.append(_entry(ts))

    rows = await store.newest_first(AuditQuery())

    assert [r.timestamp for r in rows] == [200.0, 300.0, 100.0]


async def test_the_query_filters_by_action_level_and_time_and_respects_the_limit() -> None:
    store = SqliteAuditStore(create_memory_engine())
    await store.append(_entry(1.0, action="a", level="OPERATOR"))
    await store.append(_entry(2.0, action="a", level="CRITICAL"))
    await store.append(_entry(3.0, action="b", level="CRITICAL"))
    await store.append(_entry(4.0, action="a", level="CRITICAL"))

    by_action = await store.newest_first(AuditQuery(action_name="a"))
    by_level = await store.newest_first(AuditQuery(danger_level="CRITICAL"))
    by_time = await store.newest_first(AuditQuery(since=2.0, until=3.0))
    limited = await store.newest_first(AuditQuery(limit=2))

    assert [r.timestamp for r in by_action] == [4.0, 2.0, 1.0]
    assert [r.timestamp for r in by_level] == [4.0, 3.0, 2.0]
    assert [r.timestamp for r in by_time] == [3.0, 2.0]
    assert [r.timestamp for r in limited] == [4.0, 3.0]
