"""`wf.start(thread)` runs that thread when the workflow is submitted; a `wf.thread` that nothing asks for never runs."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import build_world, close_world, new_lab, plate_template, run_to_end, speeds

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_both_started_threads_run_and_the_unasked_thread_does_not() -> None:
    lab = new_lab()
    first, second, spare = plate_template("first"), plate_template("second"), plate_template("spare")

    @orca.action(device=lab.shaker_1, inputs=[first])
    async def shake_first(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.shaker_1, inputs=[second])
    async def shake_second(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.action(device=lab.shaker_1, inputs=[spare])
    async def shake_spare(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=300)

    @orca.method
    async def first_method(ctx: MethodContext):
        yield shake_first

    @orca.method
    async def second_method(ctx: MethodContext):
        yield shake_second

    @orca.method
    async def spare_method(ctx: MethodContext):
        yield shake_spare

    @orca.thread(labware=first, start=("stacker", DISPENSE), end="waste")
    async def first_journey(ctx: ThreadContext):
        yield first_method

    @orca.thread(labware=second, start=("stacker", DISPENSE), end="waste")
    async def second_journey(ctx: ThreadContext):
        yield second_method

    @orca.thread(labware=spare, start=("stacker", DISPENSE), end="waste")
    async def spare_journey(ctx: ThreadContext):
        yield spare_method

    @orca.workflow(name="two_starts")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(first_journey)
        wf.start(second_journey)
        wf.thread(spare_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id, ended = await run_to_end(world)

        assert sorted(ended.values()) == ["COMPLETED", "COMPLETED"], ended
        assert sorted(str(t.labware_template_name) for t in world.runtime.list_threads(execution_id)) == ["first", "second"]
        assert sorted(speeds(world, "shaker_1")) == [100.0, 200.0]
    finally:
        await close_world(world)
