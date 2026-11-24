"""`ctx.param` returns the submitted value, or the default, and that value reaches the device command."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.spawn import DISPENSE
from orca.variables import VariableDefinition
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    Lab,
    build_world,
    close_world,
    commands,
    new_lab,
    plate_template,
    run_to_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _shake_at_the_speed_param(lab: Lab) -> WorkflowTemplate:
    plate = plate_template()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=await ctx.param("speed", int))

    @orca.method
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield shake_method

    @orca.workflow(name="param_speed")
    def workflow(wf: WorkflowContext) -> None:
        wf.variable("speed", VariableDefinition(type="int", default=400))
        wf.start(plate_journey)

    return workflow


@pytest.mark.parametrize(("submitted", "speed"), [({"speed": 750}, 750.0), (None, 400.0)])
async def test_the_shaker_receives_the_submitted_speed_or_the_default(
    submitted: dict[str, int] | None, speed: float,
) -> None:
    lab = new_lab()
    world = await build_world(lab, _shake_at_the_speed_param(lab))
    try:
        _, ended = await run_to_end(world, variables=submitted)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert [c.args["speed"] for c in commands(world, "shaker_1", "shake")] == [speed]
    finally:
        await close_world(world)
