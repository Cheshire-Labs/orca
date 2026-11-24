"""A thread step that waits for an event and runs out of time pauses; RETRY waits again, and once the event
comes the thread goes on to its next action and the run finishes."""
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.config import OrcaConfig
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wait_until,
    wire_system_map,
)


async def _runtime() -> tuple[SystemRuntime, WorkflowTemplate]:
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
        yield shake_action

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.on("late", timeout=0.2)
        yield shake_method

    workflow = WorkflowTemplate("wait_then_shake")
    workflow.add_thread(plate_thread, is_start=True)
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    return runtime, workflow


async def test_retry_after_the_wait_ran_out_waits_again_and_the_run_finishes() -> None:
    runtime, workflow = await _runtime()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(
            lambda: any(t.status == "PAUSED" for t in runtime.list_threads(submission.execution_id)), timeout=10.0,
        )
        [paused] = [t for t in runtime.list_threads(submission.execution_id) if t.status == "PAUSED"]
        executing = runtime._executions[submission.execution_id].executing_workflow
        assert executing is not None
        await executing.event_channel_registry.get_or_create("late").publish(value="go")

        runtime.recover_thread(submission.execution_id, paused.id, RecoveryDecision.RETRY)

        status = await execution_outcome(runtime, submission, timeout=10.0)
        assert status.status == "completed", f"Expected completed, got {status.status}: {status.error}"
    finally:
        await runtime.shutdown()


async def test_after_a_retry_inside_an_action_the_thread_is_back_in_the_action() -> None:
    """RETRY of a step inside an action resumes the action body, so the thread reads EXECUTING_ACTION again, where a
    pause asked for meanwhile is honoured between steps."""
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
    async def wait_then_shake(ctx: ActionContext) -> None:
        await ctx.wait_for("lid_off", timeout=0.2)
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield wait_then_shake

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method

    workflow = WorkflowTemplate("wait_in_action")
    workflow.add_thread(plate_thread, is_start=True)
    bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    statuses: list[str] = []
    bus.subscribe_all(lambda name, ctx: statuses.append(name.rsplit(".", 1)[-1]) if name.startswith("THREAD.") else None)
    runtime = await memory_runtime(builder.get_system(), event_bus=bus)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(
            lambda: any(t.status == "PAUSED" for t in runtime.list_threads(submission.execution_id)), timeout=10.0,
        )
        [paused] = [t for t in runtime.list_threads(submission.execution_id) if t.status == "PAUSED"]
        executing = runtime._executions[submission.execution_id].executing_workflow
        assert executing is not None
        await executing.event_channel_registry.get_or_create("lid_off").publish(value="done")

        runtime.recover_thread(submission.execution_id, paused.id, RecoveryDecision.RETRY)

        assert (await execution_outcome(runtime, submission, timeout=10.0)).status == "completed"
        after_the_pause = statuses[statuses.index("PAUSED") + 1]
        assert after_the_pause == "EXECUTING_ACTION", statuses
    finally:
        await runtime.shutdown()
