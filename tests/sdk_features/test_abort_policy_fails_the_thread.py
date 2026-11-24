"""`failure_policy=ABORT`: when the action errors the thread fails, and nothing after that action runs in that thread."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    run_to_end,
    speeds,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_thread_fails_and_sends_nothing_after_the_failed_action() -> None:
    lab = new_lab()
    lab.log("shaker_1").failures["shake"] = 1
    plate = plate_template()

    @orca.action(device=lab.shaker_1, inputs=[plate], failure_policy=FailurePolicy.ABORT)
    async def first(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.shaker_2, inputs=[plate])
    async def second(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.method
    async def first_method(ctx: MethodContext):
        yield first

    @orca.method
    async def second_method(ctx: MethodContext):
        yield second

    @orca.thread(labware=plate, start="pad_1", end="pad_2")
    async def plate_journey(ctx: ThreadContext):
        yield first_method
        yield second_method

    @orca.workflow(name="abort_policy")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id, ended = await run_to_end(world)

        assert list(ended.values()) == ["FAILED"]
        assert speeds(world, "shaker_1") == [100.0]
        assert speeds(world, "shaker_2") == []
        [plate_id] = labware_of(world, execution_id, "plate")
        record = await plate_record(world, plate_id)
        assert "shaker_2" not in record.history and "pad_2" not in record.history
    finally:
        await close_world(world)
