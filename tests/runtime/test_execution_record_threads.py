"""The persisted execution record carries its threads, so a restart can still
say WHAT a run did, not just THAT it finished.

The gap this pins (state-ownership handoff appendix, finding 2): the live
runtime built thread snapshots off live objects, the terminal record kept only
the phase, and post-restart ``get-execution-detail`` answered ``threads: [],
0/0`` for a run that demonstrably moved plates.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from pathlib import Path

from orca.runtime.db.engine import create_memory_engine, create_sqlite_engine
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.execution_record_store import PersistedThread
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from tests.memory_activity import memory_runtime
from tests.test_system_runtime import _build_simple_system
from tests.unsaved_records import UnsavedRecords


def _service() -> ExecutionRecordService:
    return ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()), UnsavedRecords())


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


class TestTheRuntimeRecordsThreads:
    async def test_a_runs_thread_lands_in_the_persisted_detail_at_its_last_status(self) -> None:
        system, workflow = await _build_simple_system()
        runtime = await memory_runtime(system)
        await runtime.start()
        try:
            record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
            await runtime.wait(record.id)
            detail = await runtime.execution_records.get_detail(record.id)
            assert detail is not None
            assert [t.status for t in detail.threads] == ["COMPLETED"]
        finally:
            await runtime.shutdown(confirm=True)

    async def test_a_thread_of_a_run_the_runtime_never_accepted_is_not_recorded(self) -> None:
        store = SqliteExecutionRecordStore(create_memory_engine())
        system, _ = await _build_simple_system()
        runtime = await memory_runtime(system, execution_record_service=ExecutionRecordService(store, UnsavedRecords()))
        runtime.record_thread_status(
            "not-a-run", thread_id="t1", name="plate_1", template_name="plate_journey",
            status="RUNNING", last_error=None, pause_reason=None, group_id=None,
        )
        await runtime.execution_records.flush_pending()
        assert await store.list_threads("not-a-run") == []
        await runtime.shutdown(confirm=True)


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


class TestTheGroupRidesTheRecord:
    async def test_a_recorded_threads_group_shows_in_detail(self) -> None:
        service = _service()
        service.record_running("e1", "wf", submitted_at=_now())
        service.record_thread(
            "e1", thread_id="t1", name="plate_1", template_name="plate_journey",
            status="RUNNING", group_id="g1",
        )
        service.record_thread(
            "e1", thread_id="t2", name="tips_1", template_name="tips_journey", status="RUNNING",
        )
        detail = await service.get_detail("e1")
        assert detail is not None
        assert {t.id: t.group_id for t in detail.threads} == {"t1": "g1", "t2": None}

    async def test_the_group_survives_a_store_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "records.db"
        store_a = SqliteExecutionRecordStore(create_sqlite_engine(db_path))
        await store_a.create_schema()
        await store_a.upsert_running("e1", "wf", _now())
        await store_a.upsert_thread("e1", PersistedThread(
            thread_id="t1", name="plate_1", template_name="plate_journey", status="COMPLETED", group_id="g2",
        ))
        await store_a.aclose()

        store_b = SqliteExecutionRecordStore(create_sqlite_engine(db_path))
        try:
            assert [t.group_id for t in await store_b.list_threads("e1")] == ["g2"]
        finally:
            await store_b.aclose()


class TestAnExistingDatabaseGainsTheGroupColumn:
    async def test_a_store_opened_on_a_file_made_before_the_group_column_reads_and_writes_groups(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "state.db"
        old = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
        old_threads = sa.Table(
            "execution_threads", sa.MetaData(),
            sa.Column("execution_id", sa.String(36), primary_key=True),
            sa.Column("thread_id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("template_name", sa.String(255), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("last_error", sa.Text, nullable=True),
            sa.Column("pause_reason", sa.String(64), nullable=True),
        )
        old_threads.create(old)
        with old.begin() as conn:
            conn.execute(sa.insert(old_threads).values(
                execution_id="e1", thread_id="t1", name="plate_1", template_name="journey", status="COMPLETED",
            ))
        old.dispose()

        store = SqliteExecutionRecordStore(create_sqlite_engine(db_path))
        try:
            await store.create_schema()
            await store.upsert_thread("e1", PersistedThread(
                thread_id="t2", name="plate_2", template_name="journey", status="RUNNING", group_id="g1",
            ))
            assert {t.thread_id: t.group_id for t in await store.list_threads("e1")} == {"t1": None, "t2": "g1"}
        finally:
            await store.aclose()
