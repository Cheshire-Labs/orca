"""The persisted execution record carries its threads, so a restart can still
say WHAT a run did, not just THAT it finished.

The gap this pins (state-ownership handoff appendix, finding 2): the live
runtime built thread snapshots off live objects, the terminal record kept only
the phase, and post-restart ``get-execution-detail`` answered ``threads: [],
0/0`` for a run that demonstrably moved plates.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from orca.events.execution_context import ThreadExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.db.engine import create_memory_engine, create_sqlite_engine
from orca.runtime.execution_record import ExecutionState
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.execution_record_store import PersistedThread
from orca.runtime.execution_tracking_sink import ExecutionTrackingSink
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore


def _service() -> ExecutionRecordService:
    return ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _thread_event(
    execution_id: str, thread_id: str, name: str, status: str,
    last_error: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_name=f"THREAD.{thread_id}.{status}",
        execution_id=execution_id,
        timestamp=time.time(),
        entity_type="THREAD",
        entity_id=thread_id,
        status=status,
        context=ThreadExecutionContext(
            execution_id=execution_id,
            workflow_name="wf",
            thread_id=thread_id,
            thread_name=name,
            template_name="plate_journey",
            last_error=last_error,
        ),
    )


class TestServiceRecordsThreads:
    async def test_recorded_thread_shows_in_detail(self) -> None:
        service = _service()
        service.record_running("e1", "wf", submitted_at=_now())
        service.record_thread(
            "e1", thread_id="t1", name="plate_1", template_name="plate_journey",
            status="RUNNING",
        )
        detail = await service.get_detail("e1")
        assert detail is not None
        assert detail.total_thread_count == 1
        snap = detail.threads[0]
        assert snap.id == "t1"
        assert snap.name == "plate_1"
        assert snap.status == "RUNNING"
        assert snap.labware_id == "t1"
        assert snap.labware_name == "plate_1"

    async def test_re_recording_a_thread_upserts_latest_status(self) -> None:
        service = _service()
        service.record_running("e1", "wf", submitted_at=_now())
        service.record_thread(
            "e1", thread_id="t1", name="plate_1", template_name="plate_journey",
            status="RUNNING",
        )
        service.record_thread(
            "e1", thread_id="t1", name="plate_1", template_name="plate_journey",
            status="COMPLETED",
        )
        detail = await service.get_detail("e1")
        assert detail is not None
        assert detail.total_thread_count == 1
        assert detail.threads[0].status == "COMPLETED"

    async def test_detail_counts_derive_from_thread_statuses(self) -> None:
        service = _service()
        service.record_running("e1", "wf", submitted_at=_now())
        service.record_thread(
            "e1", thread_id="t1", name="a", template_name="tpl", status="COMPLETED",
        )
        service.record_thread(
            "e1", thread_id="t2", name="b", template_name="tpl", status="RUNNING",
        )
        detail = await service.get_detail("e1")
        assert detail is not None
        assert detail.total_thread_count == 2
        assert detail.completed_thread_count == 1
        assert detail.active_thread_count == 1

    async def test_thread_error_rides_the_record(self) -> None:
        service = _service()
        service.record_running("e1", "wf", submitted_at=_now())
        service.record_thread(
            "e1", thread_id="t1", name="a", template_name="tpl",
            status="FAILED", last_error="shaker timed out",
        )
        detail = await service.get_detail("e1")
        assert detail is not None
        assert detail.threads[0].last_error == "shaker timed out"


class TestSinkRecordsThreadEvents:
    async def test_thread_event_lands_in_the_persisted_detail(self) -> None:
        service = _service()
        sink = ExecutionTrackingSink(service)
        service.record_running("e1", "wf", submitted_at=_now())
        sink.on_event(_thread_event("e1", "t1", "plate_1", "RUNNING"))
        sink.on_event(_thread_event("e1", "t1", "plate_1", "COMPLETED"))
        detail = await service.get_detail("e1")
        assert detail is not None
        assert [t.status for t in detail.threads] == ["COMPLETED"]

    async def test_non_thread_events_do_not_create_threads(self) -> None:
        service = _service()
        sink = ExecutionTrackingSink(service)
        service.record_running("e1", "wf", submitted_at=_now())
        sink.on_event(RuntimeEvent(
            event_name="EXECUTION.e1.COMPLETED",
            execution_id="e1",
            timestamp=time.time(),
            entity_type="EXECUTION",
            entity_id="e1",
            status="COMPLETED",
            context=ThreadExecutionContext(
                execution_id="e1", workflow_name="wf",
                thread_id="x", thread_name="x", template_name="x",
            ),
        ))
        detail = await service.get_detail("e1")
        assert detail is not None
        assert detail.threads == []
        assert detail.status == ExecutionState.COMPLETED.value


class TestSqliteStoreRoundTrip:
    async def test_threads_survive_a_store_reopen(self, tmp_path: Path) -> None:
        """The restart shape: write with one engine, read with a fresh one."""
        db_path = tmp_path / "records.db"

        engine_a = create_sqlite_engine(db_path)
        store_a = SqliteExecutionRecordStore(engine_a)
        await store_a.create_schema()
        await store_a.upsert_running("e1", "wf", _now())
        await store_a.upsert_thread("e1", PersistedThread(
            thread_id="t1", name="plate_1", template_name="plate_journey",
            status="COMPLETED",
        ))
        await store_a.aclose()

        engine_b = create_sqlite_engine(db_path)
        store_b = SqliteExecutionRecordStore(engine_b)
        try:
            threads = await store_b.list_threads("e1")
            assert [t.thread_id for t in threads] == ["t1"]
            assert threads[0].status == "COMPLETED"
        finally:
            await store_b.aclose()
