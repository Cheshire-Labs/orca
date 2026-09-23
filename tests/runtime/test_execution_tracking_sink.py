"""ExecutionTrackingSink: parses lifecycle events into Service writes.

SUBMISSION.{id}.ACCEPTED -> record_running; EXECUTION.{id}.{terminal} ->
mark_terminal; unrelated events are ignored. The sink is a thin adapter; the
DB-backed behavior is covered by test_execution_record_service.
"""

import time

from orca.events.execution_context import (
    ExecutionLifecycleContext,
    SubmissionExecutionContext,
)
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.execution_tracking_sink import ExecutionTrackingSink
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore


def _service_and_sink() -> tuple[ExecutionRecordService, ExecutionTrackingSink]:
    service = ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()))
    return service, ExecutionTrackingSink(service)


def _accepted(execution_id: str, workflow_name: str = "wf") -> RuntimeEvent:
    return RuntimeEvent(
        event_name=f"SUBMISSION.{execution_id}-sub.ACCEPTED",
        execution_id=execution_id,
        timestamp=time.time(),
        entity_type="SUBMISSION",
        entity_id=f"{execution_id}-sub",
        status="ACCEPTED",
        context=SubmissionExecutionContext(
            execution_id=execution_id,
            workflow_name=workflow_name,
            submission_id=f"{execution_id}-sub",
            group_count=1,
        ),
    )


def _terminal(execution_id: str, status: str, reason: str | None = None) -> RuntimeEvent:
    return RuntimeEvent(
        event_name=f"EXECUTION.{execution_id}.{status}",
        execution_id=execution_id,
        timestamp=time.time(),
        entity_type="EXECUTION",
        entity_id=execution_id,
        status=status,
        context=ExecutionLifecycleContext(
            execution_id=execution_id, workflow_name="wf", reason=reason,
        ),
    )


async def test_accepted_event_records_running() -> None:
    service, sink = _service_and_sink()
    sink.on_event(_accepted("e1", workflow_name="smc"))
    record = await service.get_record("e1")
    assert record is not None
    assert record.workflow_name == "smc"
    assert record.status is ExecutionState.RUNNING


async def test_completed_event_marks_completed() -> None:
    service, sink = _service_and_sink()
    sink.on_event(_accepted("e1"))
    sink.on_event(_terminal("e1", "COMPLETED"))
    record = await service.get_record("e1")
    assert record is not None
    assert record.status is ExecutionState.COMPLETED


async def test_aborted_event_marks_aborted() -> None:
    # The wire emits "ABORTED" (an ExecutionState name) for a started-then-stopped
    # run, so the sink resolves it by name with no special-case table.
    service, sink = _service_and_sink()
    sink.on_event(_accepted("e1"))
    sink.on_event(_terminal("e1", "ABORTED"))
    record = await service.get_record("e1")
    assert record is not None
    assert record.status is ExecutionState.ABORTED


async def test_non_terminal_execution_event_ignored() -> None:
    # An EXECUTION event whose status names a non-terminal ExecutionState
    # (e.g. RUNNING) must not mark the record terminal.
    service, sink = _service_and_sink()
    sink.on_event(_accepted("e1"))
    sink.on_event(_terminal("e1", "RUNNING"))
    record = await service.get_record("e1")
    assert record is not None
    assert record.status is ExecutionState.RUNNING


async def test_terminal_event_persists_reason() -> None:
    service, sink = _service_and_sink()
    sink.on_event(_accepted("e1"))
    sink.on_event(_terminal("e1", "FAILED", reason="boom"))
    record = await service.get_record("e1")
    assert record is not None
    assert record.status is ExecutionState.FAILED
    assert record.error == "boom"


async def test_unrelated_events_are_ignored() -> None:
    service, sink = _service_and_sink()
    sink.on_event(
        RuntimeEvent(
            event_name="ACTION.act-1.STARTED",
            execution_id="e1",
            timestamp=time.time(),
            entity_type="ACTION",
            entity_id="act-1",
            status="STARTED",
            context=SubmissionExecutionContext(
                execution_id="e1",
                workflow_name="wf",
                submission_id="sub-1",
            ),
        )
    )
    assert await service.list_records() == []
