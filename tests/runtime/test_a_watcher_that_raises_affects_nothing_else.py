"""The system event bus is for watchers. One that raises is logged, and nothing else changes."""

import logging
import time

import pytest

from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.execution_record import ExecutionState
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_event_bus import SystemEventBus
from orca.state.records import TrackingRecord, TrackingSource
from orca.state.ops_store import SYSTEM_ID
from tests.memory_activity import memory_runtime
from tests.test_system_runtime import _build_simple_system


class _RaisingSink:
    def on_event(self, event: RuntimeEvent) -> None:
        raise RuntimeError(f"watcher broke on {event.event_name}")


class _KeepingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def on_event(self, event: RuntimeEvent) -> None:
        self.events.append(event)


def _event(name: str) -> RuntimeEvent:
    return RuntimeEvent(
        event_name=name,
        execution_id="e1",
        timestamp=time.time(),
        entity_type="TEST",
        entity_id="x",
        status="DONE",
        context=WorkflowExecutionContext(execution_id="e1", workflow_name="wf"),
    )


def test_a_raising_listener_neither_stops_the_next_one_nor_fails_the_emitter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = SystemEventBus()
    kept = _KeepingSink()
    bus.subscribe(_RaisingSink().on_event)
    bus.subscribe(kept.on_event)

    with caplog.at_level(logging.ERROR):
        bus.emit(_event("TEST.x.DONE"))

    assert [e.event_name for e in kept.events] == ["TEST.x.DONE"]
    assert any(
        r.levelno == logging.ERROR and "TEST.x.DONE" in r.getMessage() for r in caplog.records
    )


async def test_a_raising_watcher_does_not_lose_a_runs_status() -> None:
    system, workflow = await _build_simple_system()
    runtime = await memory_runtime(system)
    runtime.register_sink(_RaisingSink())
    await runtime.start()
    try:
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
        await runtime.wait(record.id)

        stored = await runtime.execution_records.get_detail(record.id)
        assert stored is not None
        assert stored.status == ExecutionState.COMPLETED.value
        assert stored.workflow_name == workflow.name
        assert [t.status for t in stored.threads] == ["COMPLETED"]
    finally:
        await runtime.shutdown(confirm=True)


async def test_a_raising_watcher_does_not_fail_an_operation_record_save() -> None:
    system, _ = await _build_simple_system()
    runtime = await memory_runtime(system)
    runtime.register_sink(_RaisingSink())
    kept = _KeepingSink()
    runtime.register_sink(kept)
    record = TrackingRecord(
        execution_id=SYSTEM_ID, action_id="a1", thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=time.time(), operations=[],
    )

    await system.ops_history.append_record(record)

    assert [e.event_name for e in kept.events] == [f"CONTENTS.{SYSTEM_ID}.SAVED"]
    await runtime.shutdown(confirm=True)

