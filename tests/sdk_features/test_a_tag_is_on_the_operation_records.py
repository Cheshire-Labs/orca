"""`action(tag=)` is on that action's operation records and on no other action's."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    operations,
    plate_template,
    run_to_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_tag_is_on_its_actions_records_and_no_others() -> None:
    lab = new_lab()
    plate = plate_template()

    @orca.action(device=lab.shaker_1, inputs=[plate], tag="weigh")
    async def tagged(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.shaker_2, inputs=[plate])
    async def untagged(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.method
    async def both(ctx: MethodContext):
        yield tagged
        yield untagged

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx: ThreadContext):
        yield both

    @orca.workflow(name="tagged")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        records = await operations(world, execution_id)
        on = {op.device_name: record.tag for record in records for op in record.operations if op.operation.value == "shake"}
        assert on["shaker_1"] == "weigh"
        assert on["shaker_2"] is None
    finally:
        await close_world(world)
