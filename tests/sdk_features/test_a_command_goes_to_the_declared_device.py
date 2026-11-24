"""`ctx.device()` sends the command to the device the action declares, and to no other device."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    build_world,
    close_world,
    commands,
    new_lab,
    plate_template,
    run_to_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_a_shake_reaches_only_the_shaker_the_action_names() -> None:
    lab = new_lab()
    plate = plate_template()

    @orca.action(device=lab.shaker_2, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=321)

    @orca.method
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield shake_method

    @orca.workflow(name="one_shake")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        _, ended = await run_to_end(world)

        assert set(ended.values()) == {"COMPLETED"}
        assert [(c.method, c.args["speed"]) for c in commands(world, "shaker_2")] == [("shake", 321.0)]
        assert commands(world, "shaker_1") == []
        assert commands(world, "lh") == []
    finally:
        await close_world(world)
