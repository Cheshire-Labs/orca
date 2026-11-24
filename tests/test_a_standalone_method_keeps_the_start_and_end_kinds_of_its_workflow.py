"""A method run on its own keeps how its workflow starts and ends each labware.

A deck-resident plate declared `LEAVE_IN_PLACE` in its workflow stays on the record after a standalone run of a
method that uses it, as it would after the workflow; a plain end takes the plate away.
"""

from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.config import OrcaConfig
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.system_runtime import ExecutionState
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.spawn import LEAVE_IN_PLACE
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


async def _listed_after_a_standalone_run(leave_in_place: bool) -> list[str]:
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
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate, start=system_map.get_location("pad1"), end=(pad2, LEAVE_IN_PLACE) if leave_in_place else pad2)
    async def journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method

    @orca.workflow(name="holds_it")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(journey)

    builder = SdkToSystemBuilder(
        name="standalone", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    try:
        record = await runtime.submit_method(
            "holds_it", "shake_method", {"plate": "pad1"}, {"plate": "pad2"}, mode=WorkflowRunMode.CHECK_RUN,
        )
        final = await runtime.wait(record.id)
        assert final.status is ExecutionState.COMPLETED
        return [s.name.split("-")[0] for s in await runtime.labware.list_all()]
    finally:
        await runtime.shutdown()


async def test_a_plate_its_workflow_leaves_in_place_stays_on_the_record() -> None:
    assert await _listed_after_a_standalone_run(leave_in_place=True) == ["plate"]


async def test_a_plate_its_workflow_takes_away_is_taken_away() -> None:
    assert await _listed_after_a_standalone_run(leave_in_place=False) == []
