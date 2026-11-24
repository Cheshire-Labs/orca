"""A background write the database refuses three times pauses its run and raises an incident.

Run status, incidents, audit rows and events are saved in the background. Each
writer tries a failing save three times. After the third failure the record is
logged, its run is paused, and an incident says which record could not be
saved. A save that works on a later try raises nothing. The writer keeps saving
the records after the one it gave up on.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TypeVar

import pytest

from orca.events.execution_context import IncidentContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.audit_store import AUDIT_LOGGER_NAME, AuditEntry
from orca.runtime.danger import DangerLevel, write_audit
from orca.runtime.db import create_memory_engine
from orca.runtime.event_store import EVENT_ATTR, EVENT_LOGGER_NAME, EventQuery, StoredEvent
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentDetail,
    IncidentSeverity,
    OtherIncidentDetail,
    RecordNotSavedDetail,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.log_store_writer import AuditWriter, EventWriter, LogStoreWriter
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.unsaved_record import IUnsavedRecordReporter, UnsavedRecord, UnsavedRecordReporter
from orca.sdk.workflow import WorkflowTemplate
from orca.state.ops_store import SYSTEM_ID
from tests.audit_capture import InMemoryAuditStore
from tests.memory_activity import memory_daemon_system
from tests.test_two_phase_stop import _HangingDevice, _submit_and_run_to_hang, hanging_owner_system

NO_WAIT = (0.0, 0.0)
T = TypeVar("T")


class _Refusals:
    """Refuses the next `left` writes with the error a locked database raises."""

    def __init__(self, left: int) -> None:
        self.left = left
        self.attempts = 0

    def check(self) -> None:
        self.attempts += 1
        if self.left > 0:
            self.left -= 1
            raise OSError("database is locked")


class _RunStatusStore(SqliteExecutionRecordStore):
    def __init__(self, refusals: _Refusals) -> None:
        super().__init__(create_memory_engine())
        self._refusals = refusals

    async def upsert_running(self, execution_id: str, workflow_name: str, submitted_at: datetime) -> None:
        self._refusals.check()
        await super().upsert_running(execution_id, workflow_name, submitted_at)


class _IncidentStore(SqliteIncidentStore):
    def __init__(self, refusals: _Refusals) -> None:
        super().__init__(create_memory_engine())
        self._refusals = refusals

    async def insert(self, incident: SystemIncident) -> None:
        self._refusals.check()
        await super().insert(incident)


class _AuditStore(InMemoryAuditStore):
    def __init__(self, refusals: _Refusals) -> None:
        super().__init__()
        self._refusals = refusals

    async def append(self, entry: AuditEntry) -> None:
        self._refusals.check()
        await super().append(entry)


class _EventStore:
    def __init__(self, refusals: _Refusals) -> None:
        self.events: list[RuntimeEvent] = []
        self._refusals = refusals

    async def append(self, event: RuntimeEvent) -> None:
        self._refusals.check()
        self.events.append(event)

    async def read(self, query: EventQuery) -> list[StoredEvent]:
        del query
        return []


class _Bench:
    """The real reporter over a real incident service, with the runs it may pause and the live stream."""

    def __init__(self, incident_refusals: int = 0, running: tuple[str, ...] = ("run-1",)) -> None:
        self.incident_writes = _Refusals(incident_refusals)
        self.incident_store = _IncidentStore(self.incident_writes)
        self.incidents = IncidentService(self.incident_store, retry_delays=NO_WAIT)
        self.reporter = UnsavedRecordReporter(self.incidents.record)
        self.incidents.report_unsaved_to(self.reporter)
        self.live: list[SystemIncident] = []
        self.incidents.on_record = self.live.append
        self.paused: list[str] = []
        self._running = running
        self.reporter.add_run_pauser(self._pause)

    def _pause(self, execution_id: str, message: str) -> bool:
        del message
        if execution_id not in self._running:
            return False
        self.paused.append(execution_id)
        return True

    async def unsaved_incidents(self) -> list[SystemIncident]:
        return await self.incidents.list(category=IncidentCategory.RECORD_NOT_SAVED)


async def _bench(incident_refusals: int = 0, running: tuple[str, ...] = ("run-1",)) -> _Bench:
    return _Bench(incident_refusals=incident_refusals, running=running)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _detail(incident: SystemIncident) -> RecordNotSavedDetail:
    assert isinstance(incident.detail, RecordNotSavedDetail)
    return incident.detail


# -- run status ---------------------------------------------------------------


async def test_run_status_refused_three_times_pauses_the_run_once_and_later_status_is_saved() -> None:
    bench = _Bench()
    store = _RunStatusStore(_Refusals(3))
    service = ExecutionRecordService(store, bench.reporter, retry_delays=NO_WAIT)

    service.record_running("run-1", "wf", submitted_at=_now())
    await service.flush_pending()
    service.record_running("run-2", "wf", submitted_at=_now())
    await service.flush_pending()

    [incident] = await bench.unsaved_incidents()
    assert incident.execution_id == "run-1"
    assert incident.recovery_action is RecoveryAction.RESUME_EXECUTION
    assert _detail(incident).record_kind == "the run's status"
    assert _detail(incident).error_type == "OSError"
    assert "run-1" in _detail(incident).record
    assert bench.paused == ["run-1"]
    assert bench.live == [incident]
    assert await store.get("run-1") is None
    assert await store.get("run-2") is not None


async def test_run_status_refused_once_is_saved_on_the_second_try_with_no_incident_and_no_pause() -> None:
    bench = _Bench()
    writes = _Refusals(1)
    store = _RunStatusStore(writes)
    service = ExecutionRecordService(store, bench.reporter, retry_delays=NO_WAIT)

    service.record_running("run-1", "wf", submitted_at=_now())
    await service.flush_pending()

    assert writes.attempts == 2
    assert await store.get("run-1") is not None
    assert await bench.unsaved_incidents() == []
    assert bench.paused == []


async def test_run_status_of_a_run_that_is_not_running_raises_the_incident_and_pauses_nothing() -> None:
    bench = _Bench(running=())
    service = ExecutionRecordService(_RunStatusStore(_Refusals(3)), bench.reporter, retry_delays=NO_WAIT)

    service.record_running("run-1", "wf", submitted_at=_now())
    await service.flush_pending()

    [incident] = await bench.unsaved_incidents()
    assert incident.recovery_action is RecoveryAction.NONE
    assert "nothing was paused" in incident.message
    assert bench.paused == []


async def test_run_status_refused_while_the_incident_store_is_down_still_pauses_and_reaches_the_live_stream() -> None:
    bench = _Bench(incident_refusals=1_000)
    service = ExecutionRecordService(_RunStatusStore(_Refusals(3)), bench.reporter, retry_delays=NO_WAIT)

    service.record_running("run-1", "wf", submitted_at=_now())
    await service.flush_pending()

    assert bench.paused == ["run-1"]
    [live] = bench.live
    assert live.category is IncidentCategory.RECORD_NOT_SAVED
    assert await bench.unsaved_incidents() == [live]
    assert await bench.incidents.get(live.id) == live


# -- incidents ----------------------------------------------------------------


def _record_an_incident(bench: _Bench, execution_id: str | None) -> SystemIncident:
    return bench.incidents.record(
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.WARNING,
        message="something happened",
        detail=OtherIncidentDetail(message_extra="x"),
        recovery_action=RecoveryAction.NONE,
        execution_id=execution_id,
    )


async def test_an_incident_refused_three_times_pauses_its_run_once_and_later_incidents_are_saved() -> None:
    bench = _Bench(incident_refusals=3)

    lost = _record_an_incident(bench, "run-1")
    await bench.incidents.flush_pending()
    later = _record_an_incident(bench, "run-1")
    await bench.incidents.flush_pending()

    [report] = await bench.unsaved_incidents()
    assert report.execution_id == "run-1"
    assert _detail(report).record_kind == "an incident"
    assert lost.id in _detail(report).record
    assert bench.paused == ["run-1"]
    assert await bench.incident_store.get(lost.id) is None
    assert await bench.incident_store.get(later.id) == later
    assert await bench.incident_store.get(report.id) == report
    assert lost in await bench.incidents.list()


async def test_an_incident_refused_once_is_saved_on_the_second_try_with_no_report_and_no_pause() -> None:
    bench = _Bench(incident_refusals=1)

    incident = _record_an_incident(bench, "run-1")
    await bench.incidents.flush_pending()

    assert bench.incident_writes.attempts == 2
    assert await bench.incident_store.get(incident.id) == incident
    assert await bench.unsaved_incidents() == []
    assert bench.paused == []


async def test_an_incident_with_no_run_raises_the_report_and_pauses_nothing() -> None:
    bench = _Bench(incident_refusals=3)

    _record_an_incident(bench, None)
    await bench.incidents.flush_pending()

    [report] = await bench.unsaved_incidents()
    assert report.execution_id is None
    assert "belongs to no run" in report.message
    assert bench.paused == []


async def test_with_the_incident_store_down_the_report_is_not_reported_again() -> None:
    bench = _Bench(incident_refusals=1_000)

    lost = _record_an_incident(bench, "run-1")
    await bench.incidents.flush_pending()

    assert bench.paused == ["run-1"]
    assert [incident.category for incident in bench.live] == [IncidentCategory.OTHER, IncidentCategory.RECORD_NOT_SAVED]
    assert await bench.incidents.list() == bench.live
    assert lost in bench.live
    # Three tries for the incident. Its report gets one try in the same pass, and no report of its own.
    assert bench.incident_writes.attempts == 4


async def test_the_incident_saver_keeps_running_after_a_save_it_gave_up_on() -> None:
    bench = _Bench(incident_refusals=3)
    bench.incidents.start_drain_task()
    try:
        _record_an_incident(bench, "run-1")
        later = _record_an_incident(bench, "run-1")
        for _ in range(500):
            if not bench.incidents.has_pending:
                break
            await asyncio.sleep(0.005)
        assert bench.incidents.is_draining
    finally:
        await bench.incidents.stop_drain_task()

    assert await bench.incident_store.get(later.id) == later


async def test_an_unsaved_incident_can_be_acknowledged() -> None:
    bench = _Bench(incident_refusals=1_000)
    lost = _record_an_incident(bench, None)
    await bench.incidents.flush_pending()

    bench.incidents.acknowledge(lost.id)

    assert lost.id not in [incident.id for incident in await bench.incidents.list(unacknowledged_only=True)]
    assert (await bench.incidents.get(lost.id)).acknowledged is True


# -- audit rows and events ----------------------------------------------------


class _LoopThread:
    """An event loop on its own thread, as a server's loop is to the writers' listener threads."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

        self._benches: list[_Bench] = []

    def run(self, coroutine: Coroutine[None, None, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=30)

    def bench(self, incident_refusals: int = 0, running: tuple[str, ...] = ("run-1",)) -> _Bench:
        bench = self.run(_bench(incident_refusals, running))
        self._benches.append(bench)
        return bench

    def close(self) -> None:
        for bench in self._benches:
            bench.incident_writes.left = 0
            self.run(bench.incidents.aclose())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()


@pytest.fixture
def server() -> Iterator[_LoopThread]:
    thread = _LoopThread()
    try:
        yield thread
    finally:
        thread.close()


@contextmanager
def _writing(writer: LogStoreWriter, server: _LoopThread, logger_name: str) -> Iterator[None]:
    logger = logging.getLogger(logger_name)
    level = logger.level
    writer.start()
    try:
        yield
    finally:
        server.run(writer.stop())
        logger.setLevel(level)


def _settled(server: _LoopThread, writer: LogStoreWriter, bench: _Bench) -> list[SystemIncident]:
    """Every record handled, every report made, and the incidents that came of them."""
    writer.wait_until_written()
    return server.run(bench.unsaved_incidents())


def _audit(plate: str, execution_id: str | None) -> None:
    if execution_id is None:
        write_audit("test.unsaved.move", DangerLevel.CRITICAL, "because", {"plate": plate})
    else:
        write_audit("test.unsaved.move", DangerLevel.CRITICAL, "because", {"plate": plate, "execution_id": execution_id})


def test_an_audit_row_refused_three_times_pauses_its_run_once_and_later_rows_are_saved(server: _LoopThread) -> None:
    bench = server.bench()
    store = _AuditStore(_Refusals(3))
    writer = AuditWriter(store, server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, AUDIT_LOGGER_NAME):
        _audit("p1", "run-1")
        _audit("p2", "run-1")

        [incident] = _settled(server, writer, bench)

    assert incident.execution_id == "run-1"
    assert _detail(incident).record_kind == "an audit row"
    assert '"plate":"p1"' in _detail(incident).record
    assert bench.paused == ["run-1"]
    assert [entry.call_args["plate"] for entry in store.entries] == ["p2"]


def test_an_audit_row_refused_once_is_saved_on_the_second_try_with_no_incident_and_no_pause(
    server: _LoopThread,
) -> None:
    bench = server.bench()
    writes = _Refusals(1)
    store = _AuditStore(writes)
    writer = AuditWriter(store, server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, AUDIT_LOGGER_NAME):
        _audit("p1", "run-1")

        assert _settled(server, writer, bench) == []

    assert writes.attempts == 2
    assert [entry.call_args["plate"] for entry in store.entries] == ["p1"]
    assert bench.paused == []


def test_an_audit_row_for_a_command_outside_a_run_raises_the_incident_and_pauses_nothing(server: _LoopThread) -> None:
    bench = server.bench()
    writer = AuditWriter(_AuditStore(_Refusals(3)), server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, AUDIT_LOGGER_NAME):
        _audit("p1", None)

        [incident] = _settled(server, writer, bench)

    assert incident.execution_id is None
    assert incident.recovery_action is RecoveryAction.NONE
    assert bench.paused == []


def test_an_audit_row_refused_while_the_incident_store_is_down_still_pauses_and_reaches_the_live_stream(
    server: _LoopThread,
) -> None:
    bench = server.bench(incident_refusals=1_000)
    writer = AuditWriter(_AuditStore(_Refusals(3)), server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, AUDIT_LOGGER_NAME):
        _audit("p1", "run-1")

        listed = _settled(server, writer, bench)

    assert bench.paused == ["run-1"]
    [live] = bench.live
    assert live.category is IncidentCategory.RECORD_NOT_SAVED
    assert listed == [live]


def _event(execution_id: str, name: str = "INCIDENT.i1.OTHER") -> RuntimeEvent:
    return RuntimeEvent(
        event_name=name,
        execution_id=execution_id,
        timestamp=time.time(),
        entity_type="INCIDENT",
        entity_id="i1",
        status="OTHER",
        context=IncidentContext(
            incident_id="i1", category="OTHER", severity="INFO", message=name,
            recovery_action="NONE", execution_id=None, thread_id=None,
        ),
    )


def _log(event: RuntimeEvent) -> None:
    logging.getLogger(EVENT_LOGGER_NAME).info(event.event_name, extra={EVENT_ATTR: event})


def test_an_event_refused_three_times_pauses_its_run_once_and_later_events_are_saved(server: _LoopThread) -> None:
    bench = server.bench()
    store = _EventStore(_Refusals(3))
    writer = EventWriter(store, server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, EVENT_LOGGER_NAME):
        _log(_event("run-1", "first"))
        _log(_event("run-1", "second"))

        [incident] = _settled(server, writer, bench)

    assert incident.execution_id == "run-1"
    assert _detail(incident).record_kind == "an event"
    assert "first" in _detail(incident).record
    assert bench.paused == ["run-1"]
    assert [event.event_name for event in store.events] == ["second"]


def test_an_event_refused_once_is_saved_on_the_second_try_with_no_incident_and_no_pause(server: _LoopThread) -> None:
    bench = server.bench()
    writes = _Refusals(1)
    store = _EventStore(writes)
    writer = EventWriter(store, server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, EVENT_LOGGER_NAME):
        _log(_event("run-1"))

        assert _settled(server, writer, bench) == []

    assert writes.attempts == 2
    assert len(store.events) == 1
    assert bench.paused == []


def test_an_event_of_no_run_raises_the_incident_and_pauses_nothing(server: _LoopThread) -> None:
    bench = server.bench(running=(SYSTEM_ID,))
    writer = EventWriter(_EventStore(_Refusals(3)), server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, EVENT_LOGGER_NAME):
        _log(_event(SYSTEM_ID))

        [incident] = _settled(server, writer, bench)

    assert incident.execution_id is None
    assert bench.paused == []


def test_an_event_refused_while_the_incident_store_is_down_still_pauses_and_reaches_the_live_stream(
    server: _LoopThread,
) -> None:
    bench = server.bench(incident_refusals=1_000)
    writer = EventWriter(_EventStore(_Refusals(3)), server.loop, bench.reporter, retry_delays=NO_WAIT)
    with _writing(writer, server, EVENT_LOGGER_NAME):
        _log(_event("run-1"))

        listed = _settled(server, writer, bench)

    assert bench.paused == ["run-1"]
    [live] = bench.live
    assert listed == [live]


def test_the_event_of_a_could_not_save_incident_is_not_reported_when_it_cannot_be_saved(server: _LoopThread) -> None:
    bench = server.bench()
    writer = EventWriter(_EventStore(_Refusals(3)), server.loop, bench.reporter, retry_delays=NO_WAIT)
    event = _event("run-1").model_copy(update={"status": IncidentCategory.RECORD_NOT_SAVED.value})
    with _writing(writer, server, EVENT_LOGGER_NAME):
        _log(event)

        assert _settled(server, writer, bench) == []

    assert writer.dropped == 1
    assert bench.paused == []


# -- the one place that decides -----------------------------------------------


def _an_incident_service_that_is_gone(
    category: IncidentCategory,
    severity: IncidentSeverity,
    message: str,
    detail: IncidentDetail,
    recovery_action: RecoveryAction,
    execution_id: str | None = None,
    thread_id: str | None = None,
) -> SystemIncident:
    raise RuntimeError("the incident service is gone")


def test_the_report_logs_the_record_at_error_level_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    reporter = UnsavedRecordReporter(_an_incident_service_that_is_gone)

    with caplog.at_level(logging.ERROR, logger="orca.runtime.unsaved_record"):
        reporter.report(UnsavedRecord(kind="an event", record='{"n": 1}', execution_id=None), OSError("disk full"))

    first = caplog.records[0]
    assert first.levelno == logging.ERROR
    assert '{"n": 1}' in first.getMessage() and "disk full" in first.getMessage()


# -- a real run ---------------------------------------------------------------


async def _a_runtime_and_its_reporter() -> tuple[SystemRuntime, IUnsavedRecordReporter, WorkflowTemplate, _HangingDevice]:
    system, workflow, device, event_bus = await hanging_owner_system()
    injected = await memory_daemon_system(system, event_bus=event_bus)
    assert isinstance(injected.runtime, SystemRuntime)
    return injected.runtime, injected.activity.unsaved_records, workflow, device


async def test_a_running_run_is_paused_as_the_operators_pause_does_and_pauses_again_after_a_resume() -> None:
    runtime, unsaved_records, workflow, device = await _a_runtime_and_its_reporter()
    execution_id = await _submit_and_run_to_hang(runtime, workflow, device)
    status_writes = _Refusals(3)
    writer = ExecutionRecordService(
        _RunStatusStore(status_writes), unsaved_records, retry_delays=NO_WAIT,
    )
    try:
        writer.record_running(execution_id, workflow.name, submitted_at=_now())
        await writer.flush_pending()

        status = runtime.get_execution_status(execution_id)
        assert status.paused is True
        assert status.pause_reason == "system"
        [incident] = await runtime.incidents.list(category=IncidentCategory.RECORD_NOT_SAVED)
        assert incident.execution_id == execution_id
        [blocker] = [b for b in await runtime.blockers() if b.execution_id == execution_id and b.incident_id]
        assert blocker.incident_id == incident.id
        assert "could not save the run's status" in blocker.headline

        runtime.resume_execution(execution_id)
        assert runtime.get_execution_status(execution_id).paused is False

        status_writes.left = 3
        writer.record_running(execution_id, workflow.name, submitted_at=_now())
        await writer.flush_pending()

        assert runtime.get_execution_status(execution_id).paused is True
        assert len(await runtime.incidents.list(category=IncidentCategory.RECORD_NOT_SAVED)) == 2
    finally:
        device.release.set()
        await runtime.shutdown(confirm=True)
        await writer.aclose()


async def test_a_run_that_has_ended_is_not_paused() -> None:
    runtime, unsaved_records, workflow, device = await _a_runtime_and_its_reporter()
    execution_id = await _submit_and_run_to_hang(runtime, workflow, device)
    device.release.set()
    await runtime.wait(execution_id)
    writer = ExecutionRecordService(
        _RunStatusStore(_Refusals(3)), unsaved_records, retry_delays=NO_WAIT,
    )
    try:
        writer.record_running(execution_id, workflow.name, submitted_at=_now())
        await writer.flush_pending()

        assert runtime.get_execution_status(execution_id).paused is False
        [incident] = await runtime.incidents.list(category=IncidentCategory.RECORD_NOT_SAVED)
        assert "nothing was paused" in incident.message
    finally:
        await runtime.shutdown(confirm=True)
        await writer.aclose()
