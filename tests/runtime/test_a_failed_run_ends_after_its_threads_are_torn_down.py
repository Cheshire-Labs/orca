"""A run whose entry thread fails ends only after its other threads are torn down, so its end event is stored last."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

import orca.runtime.system_runtime as system_runtime_module

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.events.runtime_event import RuntimeEvent
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.event_store import EventQuery, IEventStore, StoredEvent
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.log_store_writer import EventWriter
from orca.runtime.package_import import PackageLoadError
from orca.runtime.runtime_holder import RuntimeHolder
from orca.runtime.server_events import ServerEvents
from orca.runtime.startup import ConfiguredPlugins, RuntimeSettings
from orca.runtime.sinks import LogSink
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.system_runtime import ExecutionState
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system import System
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_daemon_system, memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.real_device_path import the_real_device_path
from tests.test_helpers import create_test_plate_template, create_test_transporter, wait_until, wire_system_map

SLOW_TEARDOWN_S = 0.5


class _RecordingStore(IEventStore):
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def append(self, event: RuntimeEvent) -> None:
        self.events.append(event)

    async def read(self, query: EventQuery) -> list[StoredEvent]:
        return []


async def _a_failing_entry_thread_and_a_slow_to_stop_spawn(
    fail_now: asyncio.Event, event_bus: EventBus, stopping: asyncio.Event | None = None,
) -> System:
    shaker1, shaker2 = UniversalMockDevice("shaker1"), UniversalMockDevice("shaker2")
    transporter = create_test_transporter("robot1", ["shaker1", "shaker2", "pad1", "pad2"])
    broken, waiting = create_test_plate_template("broken"), create_test_plate_template("waiting")
    registry = ResourceRegistry()
    registry.add_resource(shaker1)
    registry.add_resource(shaker2)
    registry.add_resource(transporter)
    pool1, pool2 = ResourcePool("shaker1", [shaker1]), ResourcePool("shaker2", [shaker2])
    registry.add_resource_pool(pool1)
    registry.add_resource_pool(pool2)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shaker1, "shaker2": shaker2}, pads=["pad1", "pad2"])

    @orca.action(device=pool1, inputs=[broken], failure_policy=FailurePolicy.ABORT)
    async def fail(ctx: ActionContext) -> None:
        await fail_now.wait()
        raise RuntimeError("the author's own code failed")

    @orca.action(device=pool2, inputs=[waiting])
    async def wait_forever(ctx: ActionContext) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            # Stopping this thread takes a while, so its last events fire late.
            if stopping is None:
                await asyncio.sleep(SLOW_TEARDOWN_S)
            else:
                await stopping.wait()

    @orca.method
    async def fail_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield fail

    @orca.method
    async def wait_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield wait_forever

    pad1, pad2 = system_map.get_location("pad1"), system_map.get_location("pad2")

    @orca.thread(labware=broken, start=pad1, end=pad1)
    async def broken_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield fail_method

    @orca.thread(labware=waiting, start=pad2, end=pad2)
    async def waiting_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield wait_method

    workflow = WorkflowTemplate("fails")
    workflow.add_thread(broken_journey, is_start=True)
    workflow.add_thread(waiting_journey)
    builder = SdkToSystemBuilder(
        name="fails", description="", labwares=[broken, waiting], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def test_a_failed_run_stores_every_thread_event_before_its_end_event() -> None:
    fail_now = asyncio.Event()
    event_bus = EventBus()
    system = await _a_failing_entry_thread_and_a_slow_to_stop_spawn(fail_now, event_bus)
    injected = await memory_daemon_system(system, event_bus=event_bus)
    runtime = injected.runtime
    store = _RecordingStore()
    writer = EventWriter(store, asyncio.get_running_loop(), injected.activity.unsaved_records)
    writer.start()
    injected.activity.event_saves.add(writer)
    runtime.register_sink(LogSink())
    await runtime.start()
    try:
        record = await runtime.submit_workflow("fails", mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(lambda: len(runtime.get_execution_detail(record.id).threads) >= 1, timeout=10.0)
        spawned = await runtime.threads.spawn_thread(record.id, "waiting", confirm=True)
        await wait_until(
            lambda: any(
                t.id == spawned.id and t.status == "EXECUTING_ACTION" for t in runtime.get_execution_detail(record.id).threads
            ),
            timeout=10.0,
        )
        fail_now.set()
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=20.0)

        assert final.status is ExecutionState.FAILED
        run_events = [e.event_name for e in store.events if e.execution_id == record.id]
        end = f"EXECUTION.{record.id}.FAILED"
        assert end in run_events, run_events
        after_the_end = run_events[run_events.index(end) + 1:]
        assert after_the_end == [], f"stored after the run's end event: {after_the_end}"
        assert any(name.startswith(f"THREAD.{spawned.id}.") and name.endswith(".STOPPED") for name in run_events), (
            f"the torn-down thread's end is stored: {[n for n in run_events if n.startswith(f'THREAD.{spawned.id}.')]}"
        )
    finally:
        fail_now.set()
        await runtime.shutdown()
        await writer.stop()


async def test_a_teardown_that_never_finishes_still_ends_the_run_failed_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_runtime_module, "END_EVENTS_SAVE_TIMEOUT_S", 0.5)
    fail_now, never = asyncio.Event(), asyncio.Event()
    event_bus = EventBus()
    system = await _a_failing_entry_thread_and_a_slow_to_stop_spawn(fail_now, event_bus, stopping=never)
    injected = await memory_daemon_system(system, event_bus=event_bus)
    runtime = injected.runtime
    await runtime.start()
    try:
        record = await runtime.submit_workflow("fails", mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(lambda: len(runtime.get_execution_detail(record.id).threads) >= 1, timeout=10.0)
        spawned = await runtime.threads.spawn_thread(record.id, "waiting", confirm=True)
        await wait_until(
            lambda: any(
                t.id == spawned.id and t.status == "EXECUTING_ACTION" for t in runtime.get_execution_detail(record.id).threads
            ),
            timeout=10.0,
        )
        fail_now.set()
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert final.status is ExecutionState.FAILED
        incidents = await runtime.incidents.list(category=IncidentCategory.RECORD_NOT_SAVED)
        assert [i.execution_id for i in incidents] == [record.id], incidents
        assert "threads did not stop in time" in incidents[0].message, incidents[0].message
    finally:
        never.set()
        fail_now.set()
        await runtime.shutdown()


def _holding(runtime: ISystemRuntime, package_dir: Path) -> RuntimeHolder:
    return RuntimeHolder(
        package_dir=package_dir, setup=Mock(), activity=MagicMock(), connections=Mock(), sinks=(),
        labware_events=ServerEvents(), device_factory=the_real_device_path().device_factory,
        plugins=ConfiguredPlugins(),
        settings=RuntimeSettings(
            default_sim_speedup=None, start_timeout=120.0, stall_check_interval=None, stall_required_stable_ticks=2,
        ),
        check_run_stores=Mock(), runtime=runtime,
    )


async def test_a_rebuild_waits_while_a_failed_runs_threads_are_still_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_now, stopped = asyncio.Event(), asyncio.Event()
    event_bus = EventBus()
    system = await _a_failing_entry_thread_and_a_slow_to_stop_spawn(fail_now, event_bus, stopping=stopped)
    injected = await memory_daemon_system(system, event_bus=event_bus)
    runtime = injected.runtime
    holder = _holding(runtime, tmp_path)
    store = _RecordingStore()
    writer = EventWriter(store, asyncio.get_running_loop(), injected.activity.unsaved_records)
    writer.start()
    injected.activity.event_saves.add(writer)
    runtime.register_sink(LogSink())
    await runtime.start()
    try:
        record = await runtime.submit_workflow("fails", mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(lambda: len(runtime.get_execution_detail(record.id).threads) >= 1, timeout=10.0)
        spawned = await runtime.threads.spawn_thread(record.id, "waiting", confirm=True)
        await wait_until(
            lambda: any(
                t.id == spawned.id and t.status == "EXECUTING_ACTION" for t in runtime.get_execution_detail(record.id).threads
            ),
            timeout=10.0,
        )
        fail_now.set()
        await wait_until(lambda: runtime.get_execution(record.id).status is ExecutionState.FAILED, timeout=10.0)

        assert not holder.has_active_executions(), "a run that reads FAILED is not refused"
        waiting = asyncio.Event()
        wait_for_runs_to_end = runtime.wait_for_runs_to_end

        async def note_the_wait() -> None:
            waiting.set()
            await wait_for_runs_to_end()

        monkeypatch.setattr(runtime, "wait_for_runs_to_end", note_the_wait)
        rebuild = asyncio.create_task(holder.rebuild())
        await asyncio.wait_for(waiting.wait(), timeout=10.0)
        assert not rebuild.done(), "the rebuild waits for the failed run's threads to stop"

        stopped.set()
        with pytest.raises(PackageLoadError):
            await asyncio.wait_for(rebuild, timeout=10.0)
        run_events = [e.event_name for e in store.events if e.execution_id == record.id]
        assert any(name.startswith(f"THREAD.{spawned.id}.") and name.endswith(".STOPPED") for name in run_events), (
            "the rebuild cut the stopping thread short: its STOPPED event was never stored"
        )
        assert run_events[-1] == f"EXECUTION.{record.id}.FAILED", run_events[-5:]
    finally:
        stopped.set()
        fail_now.set()
        await runtime.shutdown()
        await writer.stop()
