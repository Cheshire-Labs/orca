"""ExecutionRecordService: off-loop writes, read-your-writes, ExecutionState.

Mirrors the IncidentService off-loop template: sync ``record_running`` /
``mark_terminal`` enqueue; async reads flush first. The persisted status IS an
ExecutionState, so the Service maps a row to the runtime record by field copy.
"""

import asyncio
import logging
from datetime import datetime, timezone


from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from tests.unsaved_records import UnsavedRecords


class _StoreThatRefuses(SqliteExecutionRecordStore):
    """Refuses every insert for `broken`, and the first `flaky_failures` inserts for any other id."""

    def __init__(self, broken: str, flaky_failures: int = 0) -> None:
        super().__init__(create_memory_engine())
        self._broken = broken
        self._flaky_failures = flaky_failures

    async def upsert_running(
        self, execution_id: str, workflow_name: str, submitted_at: datetime
    ) -> None:
        if execution_id == self._broken:
            raise OSError("database is down")
        if self._flaky_failures > 0:
            self._flaky_failures -= 1
            raise OSError("database blinked")
        await super().upsert_running(execution_id, workflow_name, submitted_at)


def _service() -> ExecutionRecordService:
    return ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()), UnsavedRecords())


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_record_running_then_get_is_running() -> None:
    service = _service()
    service.record_running("e1", "wf", submitted_at=_now())
    record = await service.get_record("e1")
    assert record is not None
    assert record.id == "e1"
    assert record.workflow_name == "wf"
    assert record.status is ExecutionState.RUNNING


async def test_mark_terminal_persists_the_state() -> None:
    cases = [
        ("e-c", ExecutionState.COMPLETED),
        ("e-f", ExecutionState.FAILED),
        ("e-a", ExecutionState.ABORTED),
    ]
    service = _service()
    for execution_id, state in cases:
        service.record_running(execution_id, "wf", submitted_at=_now())
        service.mark_terminal(execution_id, state, reason=None, terminal_at=_now())
    for execution_id, state in cases:
        record = await service.get_record(execution_id)
        assert record is not None
        assert record.status is state


async def test_first_submission_wins() -> None:
    service = _service()
    service.record_running("dup", "original", submitted_at=_now())
    service.record_running("dup", "second", submitted_at=_now())
    records = await service.list_records()
    assert [r.id for r in records] == ["dup"]
    assert records[0].workflow_name == "original"


async def test_list_records_newest_first() -> None:
    service = _service()
    older = datetime(2020, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2021, 1, 1, tzinfo=timezone.utc)
    service.record_running("old", "wf", submitted_at=older)
    service.record_running("new", "wf", submitted_at=newer)
    records = await service.list_records()
    assert [r.id for r in records] == ["new", "old"]


async def test_list_non_terminal_excludes_terminal() -> None:
    service = _service()
    service.record_running("live", "wf", submitted_at=_now())
    service.record_running("done", "wf", submitted_at=_now())
    service.mark_terminal("done", ExecutionState.COMPLETED, reason=None, terminal_at=_now())
    rows = await service.list_non_terminal()
    assert sorted(r.execution_id for r in rows) == ["live"]


async def test_mark_interrupted_lands_failed_with_reason() -> None:
    service = _service()
    service.record_running("e1", "wf", submitted_at=_now())
    await service.mark_interrupted("e1", reason="restart lost it", terminal_at=_now())
    record = await service.get_record("e1")
    assert record is not None
    assert record.status is ExecutionState.FAILED
    assert record.error == "restart lost it"


async def test_get_detail_returns_empty_threads() -> None:
    service = _service()
    service.record_running("e1", "wf", submitted_at=_now())
    service.mark_terminal("e1", ExecutionState.COMPLETED, reason=None, terminal_at=_now())
    detail = await service.get_detail("e1")
    assert detail is not None
    assert detail.id == "e1"
    assert detail.status == ExecutionState.COMPLETED.value
    assert detail.threads == []
    assert detail.total_thread_count == 0
    assert detail.completed_thread_count == 0
    assert detail.active_thread_count == 0


async def test_record_off_loop_is_visible_to_a_subsequent_read() -> None:
    service = _service()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: service.record_running("e1", "wf", _now()),
    )
    # Read-your-writes: the read flushes the queued insert to SQLite.
    record = await service.get_record("e1")
    assert record is not None
    assert record.id == "e1"


async def test_drain_task_persists_queued_items() -> None:
    store = SqliteExecutionRecordStore(create_memory_engine())
    service = ExecutionRecordService(store, UnsavedRecords())
    service.start_drain_task()
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: service.record_running("e1", "wf", _now()),
        )
        for _ in range(500):
            if not service.has_pending:
                break
            await asyncio.sleep(0.005)
        assert not service.has_pending, "background drain did not consume the record"
        await service.stop_drain_task()
        persisted = await store.get("e1")
        assert persisted is not None
        assert persisted.status is ExecutionState.RUNNING
    finally:
        await service.stop_drain_task()


async def test_stop_drain_persists_items_queued_without_a_running_drain() -> None:
    store = SqliteExecutionRecordStore(create_memory_engine())
    service = ExecutionRecordService(store, UnsavedRecords())
    service.record_running("e1", "wf", submitted_at=_now())
    assert service.has_pending
    await service.stop_drain_task()
    persisted = await store.get("e1")
    assert persisted is not None


async def test_mark_terminal_unknown_id_warns_and_creates_no_row(caplog) -> None:
    # A terminal for an id whose RUNNING row was never saved affects zero
    # rows: the store creates nothing and the Service logs a warning.
    service = _service()
    with caplog.at_level(logging.WARNING):
        service.mark_terminal("ghost", ExecutionState.FAILED, reason=None, terminal_at=_now())
        assert await service.get_record("ghost") is None
    assert any(
        "ghost" in r.getMessage() and "unknown" in r.getMessage()
        for r in caplog.records
    )


async def test_a_row_the_store_refuses_is_reported_and_does_not_stop_the_rows_after_it() -> None:
    unsaved = UnsavedRecords()
    service = ExecutionRecordService(_StoreThatRefuses(broken="bad"), unsaved, retry_delays=(0, 0))
    service.record_running("bad", "wf", submitted_at=_now())
    service.record_running("good", "wf", submitted_at=_now())

    record = await service.get_record("good")

    assert record is not None
    assert record.status is ExecutionState.RUNNING
    [(reported, error)] = unsaved.reported
    assert reported.execution_id == "bad"
    assert reported.kind == "the run's status"
    assert isinstance(error, OSError)


async def test_the_drain_keeps_saving_after_a_store_error() -> None:
    store = _StoreThatRefuses(broken="bad")
    service = ExecutionRecordService(store, UnsavedRecords(), retry_delays=(0, 0))
    service.start_drain_task()
    try:
        service.record_running("bad", "wf", submitted_at=_now())
        service.record_running("good", "wf", submitted_at=_now())
        for _ in range(500):
            if not service.has_pending:
                break
            await asyncio.sleep(0.005)
        assert not service.has_pending, "the drain stopped at the row the store refused"
    finally:
        await service.stop_drain_task()
    assert await store.get("good") is not None


async def test_a_store_error_that_clears_is_retried_and_the_row_saved() -> None:
    service = ExecutionRecordService(
        _StoreThatRefuses(broken="none", flaky_failures=1), UnsavedRecords(), retry_delays=(0, 0),
    )
    service.record_running("e1", "wf", submitted_at=_now())

    record = await service.get_record("e1")

    assert record is not None
    assert record.status is ExecutionState.RUNNING


async def test_once_the_store_looks_down_a_pass_gives_each_later_row_one_try() -> None:
    store = _StoreThatRefuses(broken="none", flaky_failures=1_000)
    service = ExecutionRecordService(store, UnsavedRecords(), retry_delays=(0, 0))
    for n in range(10):
        service.record_running(f"e{n}", "wf", submitted_at=_now())

    await service.flush_pending()

    # The first row gets every retry; the nine after it get one try each.
    assert 1_000 - store._flaky_failures == 3 + 9
