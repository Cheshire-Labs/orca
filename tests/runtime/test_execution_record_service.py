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


def _service() -> ExecutionRecordService:
    return ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()))


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
    service = ExecutionRecordService(store)
    service.start_drain_task()
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: service.record_running("e1", "wf", _now()),
        )
        for _ in range(500):
            if service._pending.empty():
                break
            await asyncio.sleep(0.005)
        assert service._pending.empty(), "background drain did not consume the record"
        await service.stop_drain_task()
        persisted = await store.get("e1")
        assert persisted is not None
        assert persisted.status is ExecutionState.RUNNING
    finally:
        if service._drain_task is not None:
            await service.stop_drain_task()


async def test_stop_drain_persists_items_queued_without_a_running_drain() -> None:
    store = SqliteExecutionRecordStore(create_memory_engine())
    service = ExecutionRecordService(store)
    service.record_running("e1", "wf", submitted_at=_now())
    assert not service._pending.empty()
    await service.stop_drain_task()
    persisted = await store.get("e1")
    assert persisted is not None


async def test_mark_terminal_unknown_id_warns_and_creates_no_row(caplog) -> None:
    # A terminal for an id never recorded (a lost SUBMISSION.ACCEPTED) affects
    # zero rows: the store creates nothing and the Service logs a warning.
    service = _service()
    with caplog.at_level(logging.WARNING):
        service.mark_terminal("ghost", ExecutionState.FAILED, reason=None, terminal_at=_now())
        assert await service.get_record("ghost") is None
    assert any(
        "ghost" in r.getMessage() and "unknown" in r.getMessage()
        for r in caplog.records
    )
