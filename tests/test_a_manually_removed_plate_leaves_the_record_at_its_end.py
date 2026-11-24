"""A plate whose thread ends with MANUAL_REMOVE leaves the record when the thread ends, in SIM as in LIVE.

In LIVE the operator's discharge removes it. In SIM nobody discharges, so the end does: the plate is no longer listed,
while its history stays readable.
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import ExecutionState
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.spawn import MANUAL_REMOVE
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


async def test_a_manually_removed_plate_is_no_longer_listed_after_a_sim_run() -> None:
    shaker = UniversalMockDevice("shaker")
    transporter = create_test_transporter("robot1", ["shaker", "pad1", "pad2"])
    plate = create_test_plate_template("plate")
    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker", [shaker])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker": shaker}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.method
    async def method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    @orca.thread(labware=plate, start=system_map.get_location("pad1"), end=(system_map.get_location("pad2"), MANUAL_REMOVE))
    async def journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    @orca.workflow(name="removed")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(journey)

    builder = SdkToSystemBuilder(
        name="removed", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    try:
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert final.status is ExecutionState.COMPLETED
        [thread] = runtime.list_threads(record.id)
        assert thread.labware_id is not None

        assert [s.name for s in await runtime.labware.list_all() if s.name.startswith("plate")] == []
        assert await runtime.labware.get_history(thread.labware_id), "its history stays readable"
    finally:
        await runtime.shutdown()
