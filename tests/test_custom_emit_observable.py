"""A ctx.emit custom event is observable on the runtime event surface.

The gap this pins (2026-08-24 live finding): ``ctx.emit`` published only to
the in-process EventChannelRegistry, the thread-coordination channel that
``ctx.wait_for`` reads. Nothing bridged it to the RuntimeEvent pipeline, so a
workflow's own domain signals (a qc verdict, a checkpoint) were invisible to
every operator surface -- /api/events, the JSONL archive, sinks -- while the
run's 83 lifecycle events all showed. The bridge must carry ALL THREE emit
sites: ``ThreadContext.emit``, ``MethodContext.emit``, ``ActionContext.emit``.
"""

from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.events.execution_context import CustomEventContext
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wire_system_map,
)


async def _run_emitting_workflow(collector: CollectorSink) -> str:
    """Run a one-thread workflow that emits from the thread, the method, AND
    an action; returns the execution id."""
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
    async def qc_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)
        await ctx.emit("qc_result", value="pass", data={"cv_percent": 3.2})

    @orca.method
    async def qc_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        await ctx.emit("method_summary", value="ok", data={"step": 1})
        yield qc_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        await ctx.emit("thread.checkpoint", value="started", data={"note": "n1"})
        yield qc_method

    workflow = WorkflowTemplate("emitting_workflow")
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

    runtime = SystemRuntime(system, event_bus=event_bus)
    runtime.register_sink(collector)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
        return submission.execution_id
    finally:
        await runtime.shutdown()


async def test_action_emit_reaches_the_event_surface() -> None:
    collector = CollectorSink()
    execution_id = await _run_emitting_workflow(collector)

    customs = [e for e in collector.events if e.entity_type == "CUSTOM"]
    qc = [e for e in customs if e.entity_id == "qc_result"]
    assert len(qc) == 1, (
        f"qc_result emit missing from the event surface; CUSTOM events: "
        f"{[e.event_name for e in customs]}"
    )
    event = qc[0]
    assert event.execution_id == execution_id
    assert event.status == "EMITTED"
    context = event.context
    assert isinstance(context, CustomEventContext)
    assert context.event_name == "qc_result"
    assert context.value == "pass"
    assert context.data == {"cv_percent": 3.2}


async def test_method_emit_reaches_the_event_surface() -> None:
    collector = CollectorSink()
    await _run_emitting_workflow(collector)

    customs = [e for e in collector.events if e.entity_type == "CUSTOM"]
    summary = [e for e in customs if e.entity_id == "method_summary"]
    assert len(summary) == 1, (
        f"method emit missing; CUSTOM events: {[e.event_name for e in customs]}"
    )
    context = summary[0].context
    assert isinstance(context, CustomEventContext)
    assert context.event_name == "method_summary"
    assert context.value == "ok"
    assert context.data == {"step": 1}


async def test_thread_emit_reaches_the_event_surface_with_its_thread() -> None:
    """The bus-safe entity id sanitizes dots; the context keeps the real
    name, and a thread-level emit names its thread."""
    collector = CollectorSink()
    await _run_emitting_workflow(collector)

    customs = [e for e in collector.events if e.entity_type == "CUSTOM"]
    checkpoint = [e for e in customs if e.entity_id == "thread_checkpoint"]
    assert len(checkpoint) == 1, (
        f"thread emit missing; CUSTOM events: {[e.event_name for e in customs]}"
    )
    context = checkpoint[0].context
    assert isinstance(context, CustomEventContext)
    assert context.event_name == "thread.checkpoint"
    assert context.value == "started"
    assert context.data == {"note": "n1"}
    assert context.thread_id is not None


async def test_emit_still_reaches_wait_for() -> None:
    """The bridge is an addition: thread coordination via wait_for keeps
    working off the channel path."""
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
    await wire_system_map(
        system_map, devices={"shaker1": device}, pads=["pad1", "pad2"],
    )

    received: list[str | None] = []

    @orca.action(device=pool, inputs=[plate1])
    async def shake1(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate2])
    async def shake2(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def method1(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake1

    @orca.method
    async def method2(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake2

    pad1_loc = system_map.get_location("pad1")
    pad2_loc = system_map.get_location("pad2")

    @orca.thread(labware=plate1, start=pad1_loc, end=pad1_loc)
    async def emitter(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        await ctx.emit("go", value="now")
        yield method1

    @orca.thread(labware=plate2, start=pad2_loc, end=pad2_loc)
    async def waiter(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        value, _data = await ctx.wait_for("go", timeout=20)
        received.append(value)
        yield method2

    workflow = WorkflowTemplate("coordinating_workflow")
    workflow.add_thread(emitter, is_start=True)
    workflow.add_thread(waiter, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate1, plate2],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()

    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await execution_outcome(runtime, submission, timeout=30.0)
    finally:
        await runtime.shutdown()

    assert received == ["now"]
