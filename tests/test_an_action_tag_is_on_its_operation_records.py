"""`action(tag=)` is written on that action's operation records, and on no other action's."""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import ExecutionState
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.mock import UniversalMockDevice
from tests.test_helpers import create_test_plate_template, create_test_transporter, wire_system_map


async def test_the_tag_is_on_its_actions_records_and_no_others() -> None:
    shaker_1 = UniversalMockDevice("shaker_1")
    shaker_2 = UniversalMockDevice("shaker_2")
    transporter = create_test_transporter("robot1", ["shaker_1", "shaker_2", "pad1"])
    plate = create_test_plate_template("plate")
    registry = ResourceRegistry()
    for resource in (shaker_1, shaker_2, transporter):
        registry.add_resource(resource)
    pool_1 = ResourcePool("shaker_1", [shaker_1])
    pool_2 = ResourcePool("shaker_2", [shaker_2])
    registry.add_resource_pool(pool_1)
    registry.add_resource_pool(pool_2)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker_1": shaker_1, "shaker_2": shaker_2}, pads=["pad1"])

    @orca.action(device=pool_1, inputs=[plate], tag="weigh")
    async def tagged(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=pool_2, inputs=[plate])
    async def untagged(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=200)

    @orca.method
    async def both(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield tagged
        yield untagged

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def plate_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield both

    @orca.workflow(name="tagged")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="tagged", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=event_bus, config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = await memory_runtime(system, event_bus=event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert final.status is ExecutionState.COMPLETED

        records = await system.ops_history.for_execution(record.id).records()
        shakes = {op.device_name: r.tag for r in records for op in r.operations if op.operation.value == "shake"}
        assert shakes == {"shaker_1": "weigh", "shaker_2": None}
    finally:
        await runtime.shutdown()
