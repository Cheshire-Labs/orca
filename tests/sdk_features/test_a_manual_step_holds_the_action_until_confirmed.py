"""`ctx.manual_step` holds the action until its own step is confirmed.

Timeout then RETRY: `test_a_manual_step_retry_waits_for_the_confirm.py`.
"""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.runtime.status_models import PendingManualStepRecord
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    FeatureWorld,
    Lab,
    build_world,
    close_world,
    events,
    new_lab,
    plate_template,
    speeds,
    submit,
    wait_for_the_end,
)
from tests.test_helpers import let_the_loop_run, wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _two_manual_steps(lab: Lab, timeout_hours: float = 0) -> WorkflowTemplate:
    first, second = plate_template("first"), plate_template("second")

    @orca.action(device=lab.shaker_1, inputs=[first])
    async def step_a(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)
        await ctx.manual_step("step A", timeout_hours=timeout_hours)
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.action(device=lab.shaker_2, inputs=[second])
    async def step_b(ctx: ActionContext) -> None:
        await ctx.manual_step("step B")
        await ctx.device(IShaker).shake(duration=1, speed=300)

    @orca.method
    async def method_a(ctx: MethodContext):
        yield step_a

    @orca.method
    async def method_b(ctx: MethodContext):
        yield step_b

    @orca.thread(labware=first, start="pad_1", end="pad_1")
    async def first_journey(ctx: ThreadContext):
        yield method_a

    @orca.thread(labware=second, start="pad_2", end="pad_2")
    async def second_journey(ctx: ThreadContext):
        yield method_b

    @orca.workflow(name="two_manual_steps")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(first_journey)
        wf.start(second_journey)

    return workflow


async def _pending(world: FeatureWorld, execution_id: str, count: int) -> dict[str, PendingManualStepRecord]:
    await wait_until(lambda: len(world.runtime.list_pending_manual_steps(execution_id)) == count, timeout=30)
    return {step.instruction: step for step in world.runtime.list_pending_manual_steps(execution_id)}


async def test_confirming_one_step_releases_only_that_step() -> None:
    lab = new_lab()
    world = await build_world(lab, _two_manual_steps(lab))
    try:
        execution_id = await submit(world)
        pending = await _pending(world, execution_id, 2)
        await let_the_loop_run()
        assert speeds(world, "shaker_1") == [100.0]
        assert speeds(world, "shaker_2") == []
        instructions = [e for e in events(world, "OPERATOR.") if e.execution_id == execution_id]
        assert len(instructions) == 2

        await world.runtime.confirm_manual_step(execution_id, pending["step A"].step_id)
        await wait_until(lambda: speeds(world, "shaker_1") == [100.0, 200.0], timeout=30)
        await let_the_loop_run()
        assert speeds(world, "shaker_2") == [], "confirming step A must not release step B"

        await world.runtime.confirm_manual_step(execution_id, pending["step B"].step_id)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_2") == [300.0]
    finally:
        await close_world(world)
