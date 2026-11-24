"""A move to a position the system knows but no mover is taught pauses at MOVE_RESOLUTION, where RETRY plans again.

It is a move with no route, the same as one whose ends no path connects.
"""
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.config import OrcaConfig
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
    wire_system_map,
)


async def test_a_move_to_a_position_no_mover_is_taught_pauses_where_retry_plans_again() -> None:
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["pad1"])
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
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method

    workflow = WorkflowTemplate("untaught")
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
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.CHECK_RUN)
        await wait_until(
            lambda: any(t.status == "PAUSED" for t in runtime.list_threads(submission.execution_id)), timeout=10.0,
        )
        [paused] = [t for t in runtime.get_execution_detail(submission.execution_id).threads if t.status == "PAUSED"]

        assert paused.pause_site == "MOVE_RESOLUTION", paused.last_error
        assert "RETRY" in paused.honoured_decisions
    finally:
        await runtime.shutdown(confirm=True)
