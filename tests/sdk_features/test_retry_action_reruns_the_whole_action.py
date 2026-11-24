"""When an action's own Python raises, `RETRY_ACTION` runs the body again from the top; a step `RETRY` is refused there."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    plate_template,
    recover,
    run_to_pause,
    speeds,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_retry_runs_the_body_again_and_resends_the_commands_before_the_raise() -> None:
    lab = new_lab()
    plate = plate_template()
    raised: list[bool] = []

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake_then_compute(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)
        if not raised:
            raised.append(True)
            raise ZeroDivisionError("the author's own arithmetic failed")
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.method
    async def compute_method(ctx: MethodContext):
        yield shake_then_compute

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def plate_journey(ctx: ThreadContext):
        yield compute_method

    @orca.workflow(name="author_error")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id, paused = await run_to_pause(world)
        assert paused.pause_reason == "error"
        assert "ZeroDivisionError" in (paused.pause_message or "") + (paused.last_error or "")
        assert speeds(world, "shaker_1") == [100.0]

        with pytest.raises(ValueError, match="use RETRY_ACTION"):
            await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY_ACTION)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [100.0, 100.0, 200.0]
    finally:
        await close_world(world)
