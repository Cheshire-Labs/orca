"""A run the last server process left running is marked failed, and each sink hears it was lost."""

from datetime import datetime, timezone

from orca.events.execution_context import ExecutionLifecycleContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.interrupted_runs import (
    INTERRUPTED_EVENT_STATUS,
    INTERRUPTED_REASON,
    mark_interrupted_executions,
)
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from tests.unsaved_records import UnsavedRecords


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def on_event(self, event: RuntimeEvent) -> None:
        self.events.append(event)


async def _records_with(*running: tuple[str, str]) -> ExecutionRecordService:
    records = ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()), UnsavedRecords())
    await records.ensure_schema()
    records.start_drain_task()
    for execution_id, workflow_name in running:
        records.record_running(execution_id, workflow_name, datetime.now(timezone.utc))
    await records.flush_pending()
    return records


async def test_a_run_left_running_is_marked_failed_with_the_reason() -> None:
    records = await _records_with(("exec-1", "assay"))

    marked = await mark_interrupted_executions(records, [])
    record = await records.get_record("exec-1")
    await records.aclose()

    assert marked == ["exec-1"]
    assert record is not None
    assert record.status == ExecutionState.FAILED
    assert record.error == INTERRUPTED_REASON


async def test_each_sink_hears_that_each_run_was_lost() -> None:
    records = await _records_with(("exec-a", "wf-a"), ("exec-b", "wf-b"))
    first, second = _RecordingSink(), _RecordingSink()

    await mark_interrupted_executions(records, [first, second])
    await records.aclose()

    for sink in (first, second):
        assert sorted(e.execution_id for e in sink.events) == ["exec-a", "exec-b"]
        assert {e.status for e in sink.events} == {INTERRUPTED_EVENT_STATUS}
        contexts = [e.context for e in sink.events]
        assert all(isinstance(c, ExecutionLifecycleContext) for c in contexts)
        assert sorted(c.workflow_name for c in contexts if isinstance(c, ExecutionLifecycleContext)) == [
            "wf-a", "wf-b",
        ]


async def test_a_finished_run_is_left_alone() -> None:
    records = await _records_with(("exec-done", "assay"))
    await records.mark_interrupted("exec-done", "finished earlier", datetime.now(timezone.utc))
    sink = _RecordingSink()

    marked = await mark_interrupted_executions(records, [sink])
    await records.aclose()

    assert marked == []
    assert sink.events == []
