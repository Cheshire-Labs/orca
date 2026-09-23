"""Tests for Event System Redesign + Sinks.

Integration tests defining the SystemRuntime event API:
event_bus wiring, sinks, system-scoped plugin delivery,
concurrent-workflow isolation, and entity resolution.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.events.execution_context import (
    ThreadExecutionContext,
    WorkflowExecutionContext,
)
from orca.plugins.base import OrcaPlugin
from orca.plugins.method_tracker import MethodTracker
from orca.plugins.labware_journey_tracker import LabwareJourneyTracker
from orca.resource_models.resource_pool import ResourcePool
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.entity_resolver import resolve_entity
from orca.runtime.sinks import CollectorSink, LogSink
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_helpers import (
    execution_outcome,
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


async def _build_simple_system_with_event_bus() -> tuple[ISystem, WorkflowTemplate, EventBus]:
    """Build a minimal sim system and return (system, workflow, event_bus)."""
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield shake_method

    workflow = WorkflowTemplate("simple_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    return system, workflow, event_bus


async def _build_concurrent_system_with_event_bus() -> tuple[ISystem, WorkflowTemplate, WorkflowTemplate, EventBus]:
    """Build a sim system with two pads so two workflows can run concurrently."""
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate1 = create_test_plate_template("plate_96")
    plate2 = create_test_plate_template("plate_97")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate1])
    async def shake_action1(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate2])
    async def shake_action2(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method_1(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action1

    @orca.method
    async def shake_method_2(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action2

    pad1_loc = system_map.get_location("pad1")
    pad2_loc = system_map.get_location("pad2")

    @orca.thread(labware=plate1, start=pad1_loc, end=pad1_loc)
    async def thread1(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield shake_method_1

    @orca.thread(labware=plate2, start=pad2_loc, end=pad2_loc)
    async def thread2(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield shake_method_2

    workflow1 = WorkflowTemplate("workflow_1")
    workflow1.add_thread(thread1, is_start=True)
    workflow2 = WorkflowTemplate("workflow_2")
    workflow2.add_thread(thread2, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate1, plate2],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow1, workflow2],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    return system, workflow1, workflow2, event_bus


class TestSystemRuntimeEventWiring:
    """Integration tests defining the SystemRuntime event API contract."""

    async def test_collector_sink_captures_execution_events(self) -> None:
        """register_sink() wires a sink to the SystemEventBus.
        Events from workflow execution flow through to the sink."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        collector = CollectorSink()
        runtime.register_sink(collector)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        assert len(collector.events) > 0
        assert all(isinstance(e, RuntimeEvent) for e in collector.events)
        assert all(e.execution_id == submission.execution_id for e in collector.events)

    async def test_unregister_sink_stops_delivery(self) -> None:
        """unregister_sink() detaches a sink so later events skip it, while a
        still-registered sink keeps receiving them."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        detached = CollectorSink()
        kept = CollectorSink()
        runtime.register_sink(detached)
        runtime.register_sink(kept)

        await runtime.start()
        first = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, first, timeout=30.0)

        assert len(detached.events) > 0
        runtime.unregister_sink(detached)
        count_at_detach = len(detached.events)

        second = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, second, timeout=30.0)
        await runtime.shutdown()

        assert any(e.execution_id == second.execution_id for e in kept.events)
        assert len(detached.events) == count_at_detach
        assert all(e.execution_id != second.execution_id for e in detached.events)

    async def test_terminal_execution_event_reaches_sink(self) -> None:
        """A registered sink must receive the terminal EXECUTION.{id}.COMPLETED
        event, not just the during-run THREAD/METHOD events. Regression for the
        forwarder unregistering the execution before _on_task_done emits."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        collector = CollectorSink()
        runtime.register_sink(collector)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        names = [e.event_name for e in collector.events]
        assert f"EXECUTION.{submission.execution_id}.COMPLETED" in names, (
            f"terminal EXECUTION event missing from sink; got: {sorted(set(names))}"
        )
        assert f"SUBMISSION.{submission.id}.COMPLETED" in names

    async def test_get_events_for_execution(self) -> None:
        """SystemRuntime.get_events_for_execution() returns events
        filtered by execution_id from the internal SystemEventBus."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        events = runtime.get_events_for_execution(submission.execution_id)
        assert len(events) > 0
        assert all(e.execution_id == submission.execution_id for e in events)

    async def test_get_events_since(self) -> None:
        """SystemRuntime.get_events_since() filters by timestamp."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)

        await runtime.start()
        before = time.time()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        events = runtime.get_events_since(before)
        assert len(events) > 0

    async def test_system_scoped_plugin_receives_events(self) -> None:
        """A MethodTracker registered on the runtime sees thread events
        without being manually registered on the workflow."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)

        tracker = MethodTracker()
        runtime.register_plugin(tracker)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        assert len(tracker._thread_names) > 0

    async def test_runtime_without_event_bus_still_works(self) -> None:
        """Backward compat: SystemRuntime works without event_bus param."""
        system, workflow, _ = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        assert status.status == "completed"

    async def test_events_contain_structured_fields(self) -> None:
        """Captured events have parsed entity_type, entity_id, status."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        collector = CollectorSink()
        runtime.register_sink(collector)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        thread_events = [e for e in collector.events if e.entity_type == "THREAD"]
        assert len(thread_events) > 0

        # Verify structured parsing: 3-part events have entity_id (UUID), status
        three_part = [e for e in thread_events if e.entity_id]
        assert len(three_part) > 0
        for e in three_part:
            assert len(e.entity_id) > 0
            assert len(e.status) > 0
            assert e.event_name == f"THREAD.{e.entity_id}.{e.status}"

    async def test_log_sink_emits_valid_json(self) -> None:
        """LogSink logs each event as a valid JSON line. default=str handles any
        non-serializable context fields without raising."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        sink = LogSink()
        runtime.register_sink(sink)

        log_records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record.getMessage())

        handler = _Capture()
        log = logging.getLogger("orca.events")
        original_level = log.level
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        try:
            await runtime.start()
            submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            await execution_outcome(runtime, submission, timeout=30.0)
            await runtime.shutdown()
        finally:
            log.removeHandler(handler)
            log.setLevel(original_level)

        assert len(log_records) > 0
        for line in log_records:
            parsed = json.loads(line)
            assert "event_name" in parsed
            assert "execution_id" in parsed
            assert "timestamp" in parsed

    async def test_concurrent_workflows_no_plugin_collision(self) -> None:
        """Two concurrent workflows produce separate events and plugin state.
        Events for exec1 must not contain thread_ids from exec2 and vice versa."""
        system, workflow1, workflow2, event_bus = await _build_concurrent_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        collector = CollectorSink()
        runtime.register_sink(collector)

        tracker = MethodTracker()
        runtime.register_plugin(tracker)

        await runtime.start()
        submission1 = await runtime.submit(workflow1, mode=WorkflowRunMode.PURE_SIM)
        submission2 = await runtime.submit(workflow2, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission1, timeout=30.0)
        await execution_outcome(runtime, submission2, timeout=30.0)
        await runtime.shutdown()

        exec1_events = [e for e in collector.events if e.execution_id == submission1.execution_id]
        exec2_events = [e for e in collector.events if e.execution_id == submission2.execution_id]
        assert len(exec1_events) > 0
        assert len(exec2_events) > 0

        # Each execution tracked its own threads (keyed by thread_id, no collision)
        assert len(tracker._thread_names) >= 2

        # Thread ids seen in exec1 must not appear in exec2's events and vice versa
        exec1_thread_ids = {
            e.context.thread_id
            for e in exec1_events
            if isinstance(e.context, ThreadExecutionContext)
        }
        exec2_thread_ids = {
            e.context.thread_id
            for e in exec2_events
            if isinstance(e.context, ThreadExecutionContext)
        }
        assert exec1_thread_ids.isdisjoint(exec2_thread_ids), (
            f"Thread id overlap between executions: {exec1_thread_ids & exec2_thread_ids}"
        )

    async def test_thread_names_have_hash_suffix(self) -> None:
        """Thread names in events carry an id-prefix suffix (the labware
        instance name shape from ``instance_name_for``)."""
        system, workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        tracker = MethodTracker()
        runtime.register_plugin(tracker)

        await runtime.start()
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        await runtime.shutdown()

        for thread_id, thread_name in tracker._thread_names.items():
            assert "-" in thread_name, f"Thread name '{thread_name}' missing hash suffix"
            suffix = thread_name.rsplit("-", 1)[-1]
            assert thread_id.startswith(suffix), (
                f"Thread name suffix '{suffix}' doesn't match id prefix '{thread_id[:4]}'"
            )


class _CountingPlugin(OrcaPlugin):
    """Counts every RuntimeEvent it receives."""

    def __init__(self) -> None:
        self.count = 0

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        del event
        self.count += 1


def _synthetic_event() -> RuntimeEvent:
    return RuntimeEvent.from_event_bus(
        "SYSTEM.TEST",
        "exec-test",
        WorkflowExecutionContext(execution_id="exec-test", workflow_name="w"),
    )


class TestPluginUnregister:
    """unregister_plugin must actually drop the bus subscription."""

    async def test_unregister_stops_event_delivery(self) -> None:
        system, _workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        plugin = _CountingPlugin()
        runtime.register_plugin(plugin)

        runtime._system_event_bus.emit(_synthetic_event())
        assert plugin.count == 1

        runtime.unregister_plugin(_CountingPlugin)

        runtime._system_event_bus.emit(_synthetic_event())
        assert plugin.count == 1, "plugin kept receiving events after unregister"

    async def test_unregister_drops_every_instance_of_the_type(self) -> None:
        system, _workflow, event_bus = await _build_simple_system_with_event_bus()
        runtime = SystemRuntime(system, event_bus=event_bus)
        first = _CountingPlugin()
        second = _CountingPlugin()
        runtime.register_plugin(first)
        runtime.register_plugin(second)

        runtime._system_event_bus.emit(_synthetic_event())
        assert first.count == 1 and second.count == 1

        runtime.unregister_plugin(_CountingPlugin)

        runtime._system_event_bus.emit(_synthetic_event())
        assert first.count == 1 and second.count == 1


class TestEntityResolver:
    """Tests for resolve_entity() lookup utility."""

    def test_exact_id_match(self) -> None:
        registry = {"abc123": "my-thread", "def456": "other-thread"}
        assert resolve_entity("abc123", registry) == "abc123"

    def test_exact_name_match(self) -> None:
        registry = {"abc123": "my-thread", "def456": "other-thread"}
        assert resolve_entity("my-thread", registry) == "abc123"

    def test_id_prefix_match(self) -> None:
        registry = {"abc123": "my-thread", "def456": "other-thread"}
        assert resolve_entity("abc", registry) == "abc123"

    def test_name_prefix_match(self) -> None:
        registry = {"abc123": "my-thread-a3f2", "def456": "other-thread-b4c1"}
        assert resolve_entity("my-thread", registry) == "abc123"

    def test_name_prefix_ambiguous_raises(self) -> None:
        registry = {"abc123": "my-thread-a3f2", "def456": "my-thread-b4c1"}
        with pytest.raises(ValueError, match="Ambiguous"):
            resolve_entity("my-thread", registry)

    def test_no_match_returns_none(self) -> None:
        registry = {"abc123": "my-thread"}
        assert resolve_entity("xyz", registry) is None

    def test_empty_registry(self) -> None:
        assert resolve_entity("anything", {}) is None
