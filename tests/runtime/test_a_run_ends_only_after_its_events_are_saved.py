"""A run is recorded as ended only after every event it fired is saved, and its end event fires last.

A reader that sees the run end must find its events. A store that stops answering must not stop the run ending:
the wait is bounded, and the run is reported as having events that were not confirmed saved.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
import orca.runtime.system_runtime as system_runtime_module
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.events.runtime_event import RuntimeEvent
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.event_store import EventQuery, IEventStore, StoredEvent
from orca.runtime.incident_store import IncidentCategory, RecoveryAction
from orca.runtime.log_store_writer import EventWriter
from orca.runtime.sinks import LogSink
from orca.runtime.system_runtime import ExecutionState
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system import System
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_daemon_system, memory_ops_store, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map


class _GatedStore(IEventStore):
    """Saves an event only once the gate is open."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.events: list[RuntimeEvent] = []

    async def append(self, event: RuntimeEvent) -> None:
        await self.gate.wait()
        self.events.append(event)

    async def read(self, query: EventQuery) -> list[StoredEvent]:
        return []


async def _one_shake() -> tuple[System, str]:
    shaker = UniversalMockDevice("shaker")
    transporter = create_test_transporter("robot1", ["shaker", "pad1"])
    plate = create_test_plate_template("plate")
    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker", [shaker])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker": shaker}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    @orca.workflow(name="one_shake")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(journey)

    builder = SdkToSystemBuilder(
        name="ends", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow.name


async def test_the_run_ends_after_its_events_are_saved_and_its_end_event_is_saved_last() -> None:
    system, workflow = await _one_shake()
    injected = await memory_daemon_system(system)
    runtime = injected.runtime
    store = _GatedStore()
    writer = EventWriter(store, asyncio.get_running_loop(), injected.activity.unsaved_records)
    writer.start()
    injected.activity.event_saves.add(writer)
    runtime.register_sink(LogSink())
    await runtime.start()
    try:
        record = await runtime.submit_workflow(workflow, mode=WorkflowRunMode.CHECK_RUN)
        for _ in range(200):
            if [t.status for t in runtime.get_execution_detail(record.id).threads] == ["COMPLETED"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the run's work did not finish")
        for _ in range(20):
            await asyncio.sleep(0)
        assert runtime.get_execution(record.id).status is ExecutionState.RUNNING, "not ended while its events wait"

        store.gate.set()
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert final.status is ExecutionState.COMPLETED
        run_events = [e.event_name for e in store.events if e.execution_id == record.id]
        assert run_events[-1] == f"EXECUTION.{record.id}.COMPLETED", run_events
    finally:
        store.gate.set()
        await runtime.shutdown()
        await writer.stop()


async def test_a_store_that_never_answers_does_not_stop_the_run_ending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_runtime_module, "END_EVENTS_SAVE_TIMEOUT_S", 0.5)
    system, workflow = await _one_shake()
    injected = await memory_daemon_system(system)
    runtime = injected.runtime
    store = _GatedStore()
    writer = EventWriter(store, asyncio.get_running_loop(), injected.activity.unsaved_records)
    writer.start()
    injected.activity.event_saves.add(writer)
    runtime.register_sink(LogSink())
    await runtime.start()
    try:
        record = await runtime.submit_workflow(workflow, mode=WorkflowRunMode.CHECK_RUN)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

        assert final.status is ExecutionState.COMPLETED
        assert runtime.get_execution(record.id).paused is False
        incidents = await runtime.incidents.list(category=IncidentCategory.RECORD_NOT_SAVED)
        assert [i.execution_id for i in incidents] == [record.id], incidents
        assert incidents[0].recovery_action is RecoveryAction.NONE
    finally:
        store.gate.set()
        await runtime.shutdown()
        await writer.stop()
