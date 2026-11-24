"""Shutdown stops the work before it closes what the work is using.

A workflow's entry threads run in tasks the execution task does not own. A
shutdown that cancelled only the execution task left them running, then closed
the stores under them: the next move died on a closed database, the thread
paused for an operator who was never coming, and nothing could end it. In CI
that surfaced as a test timing out in teardown while the loop waited on a task
that would never finish; in a deployment it is a process that will not exit.
"""
import asyncio

import pytest

from orca.gateway.controller.controller import DeviceController
from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    IncidentCategory, IncidentSeverity, OtherIncidentDetail, RecoveryAction, SystemIncident,
)
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from tests.no_devices import NullGatewayRegistry
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime import system_runtime
from orca.runtime.system_runtime import SystemRuntime
from tests.runtime.test_deck_layout_required_on_submit import build_system_with_a_laid_out_lh
from tests.memory_activity import memory_runtime
from tests.unsaved_records import UnsavedRecords


async def _submitted_then_shut_down() -> list[asyncio.Task[object]]:
    """Submit a workflow, shut down at once, and report what is still running."""
    system = await build_system_with_a_laid_out_lh()
    incidents = IncidentService(SqliteIncidentStore(create_memory_engine()))
    records = ExecutionRecordService(SqliteExecutionRecordStore(create_memory_engine()), UnsavedRecords())
    await incidents.ensure_schema()
    await records.ensure_schema()
    # The activity services' drains belong to whoever opened them, not to the runtime.
    owned_elsewhere = {
        asyncio.current_task(),
        incidents.start_drain_task(),
        records.start_drain_task(),
    }
    try:
        runtime = await memory_runtime(
            system, gateway_registry=NullGatewayRegistry(),
            incident_service=incidents, execution_record_service=records,
        )
        await runtime.start()
        try:
            await runtime.submit_workflow(
                "deck_layout_test_wf", mode=WorkflowRunMode.CHECK_RUN,
            )
        finally:
            await runtime.shutdown()
        # Read the tasks the moment shutdown returns, with nothing in between. A
        # shutdown that waited for its work has nothing left running here; one that
        # did not shows its orphans without anybody having to wait for them.
        return [
            t for t in asyncio.all_tasks()
            if t not in owned_elsewhere and not t.done() and not _is_the_orca_clients_heartbeat(t)
        ]
    finally:
        try:
            await incidents.aclose()
        finally:
            await records.aclose()


def _is_the_orca_clients_heartbeat(task: asyncio.Task[object]) -> bool:
    """The test's in-process orca-client outlives the runtime, as a real orca-client does."""
    return task.get_coro().__qualname__ == "InProcessClient._beat"


@pytest.mark.timeout(60)
async def test_shutdown_leaves_no_task_running() -> None:
    """The submit returns while the entry threads are still starting, which is
    the window a shutdown used to walk into."""
    still_running = await _submitted_then_shut_down()

    assert still_running == [], (
        "shutdown returned with work still running against stores it then "
        "closed: " + ", ".join(t.get_name() for t in still_running)
    )


@pytest.mark.timeout(60)
async def test_shutdown_gives_up_on_a_drain_that_will_not_finish(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The drain waits, and a wait in shutdown has to end. A thread that never
    comes back must cost a warning, not a process that will not exit."""
    async def never_returns(self: SystemRuntime) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(system_runtime, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.1)
    monkeypatch.setattr(
        SystemRuntime, "_abort_and_await_executions", never_returns,
    )
    system = await build_system_with_a_laid_out_lh()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())
    await runtime.start()

    with caplog.at_level("WARNING"):
        await runtime.shutdown()

    assert "running executions" in caplog.text, (
        "shutdown gave up without saying what it stopped waiting for"
    )



@pytest.mark.timeout(60)
async def test_shutdown_waits_for_a_cancel_still_going_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Aborting a device command tells the device bridge to drop it, on a task
    the unwinding caller schedules and never awaits. Shutdown is the only thing
    that can wait for it, and a wait in shutdown has to end."""
    async def never_returns(self: DeviceController) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(system_runtime, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.1)
    monkeypatch.setattr(DeviceController, "await_cancels_sent", never_returns)
    system = await build_system_with_a_laid_out_lh()
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())
    await runtime.start()

    with caplog.at_level("WARNING"):
        await runtime.shutdown()

    assert "cancels" in caplog.text, (
        "shutdown never waited for the cancels it scheduled"
    )


class _IncidentsSavedWhenLetGo(SqliteIncidentStore):
    """A database whose incident insert waits until the test lets it go."""

    def __init__(self) -> None:
        super().__init__(create_memory_engine())
        self.saving = asyncio.Event()
        self.let_go = asyncio.Event()

    async def insert(self, incident: SystemIncident) -> None:
        self.saving.set()
        await self.let_go.wait()
        await super().insert(incident)


def _an_incident(service: IncidentService) -> SystemIncident:
    return service.record(
        category=IncidentCategory.OTHER, severity=IncidentSeverity.WARNING,
        message="filed while the runtime shuts down", detail=OtherIncidentDetail(message_extra=""),
        recovery_action=RecoveryAction.NONE, execution_id=None,
    )


@pytest.mark.timeout(60)
async def test_shutdown_waits_for_an_incident_still_being_saved() -> None:
    """The incident service's drain belongs to its owner, and shutdown still does not return ahead of its write."""
    store = _IncidentsSavedWhenLetGo()
    incidents = IncidentService(store)
    await incidents.ensure_schema()
    incidents.start_drain_task()
    try:
        system = await build_system_with_a_laid_out_lh()
        runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry(), incident_service=incidents)
        await runtime.start()
        filed = _an_incident(incidents)
        await asyncio.wait_for(store.saving.wait(), timeout=10.0)

        shutdown = asyncio.create_task(runtime.shutdown())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(shutdown), timeout=2.0)
        store.let_go.set()
        await shutdown

        assert await store.get(filed.id) is not None
        assert incidents.is_draining
    finally:
        store.let_go.set()
        await incidents.aclose()


@pytest.mark.timeout(60)
async def test_shutdown_gives_up_on_an_incident_that_will_not_save(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(system_runtime, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.2)
    store = _IncidentsSavedWhenLetGo()
    incidents = IncidentService(store)
    await incidents.ensure_schema()
    incidents.start_drain_task()
    try:
        system = await build_system_with_a_laid_out_lh()
        runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry(), incident_service=incidents)
        await runtime.start()
        _an_incident(incidents)
        await asyncio.wait_for(store.saving.wait(), timeout=10.0)

        with caplog.at_level("WARNING"):
            await asyncio.wait_for(runtime.shutdown(), timeout=20.0)

        assert "incidents and execution records" in caplog.text
    finally:
        store.let_go.set()
        await incidents.aclose()
