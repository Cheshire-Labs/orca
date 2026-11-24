"""An action with no `failure_policy` pauses when its device call fails; the run's other thread finishes; a step retry re-sends only the failed command."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    plate_template,
    recover,
    speeds,
    statuses,
    submit,
    wait_for_a_pause,
    wait_for_the_end,
)
from tests.test_helpers import wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_a_failed_call_pauses_by_default_and_a_step_retry_resends_only_it() -> None:
    lab = new_lab()
    lab.log("shaker_1").fail_at["shake"] = {2}
    failing, other = plate_template("failing"), plate_template("other")

    @orca.action(device=lab.shaker_1, inputs=[failing])
    async def two_shakes(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.action(device=lab.shaker_2, inputs=[other])
    async def one_shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=300)

    assert two_shakes.failure_policy in (None, FailurePolicy.PAUSE)

    @orca.method
    async def failing_method(ctx: MethodContext):
        yield two_shakes

    @orca.method
    async def other_method(ctx: MethodContext):
        yield one_shake

    @orca.thread(labware=failing, start="pad_1", end="pad_1")
    async def failing_journey(ctx: ThreadContext):
        yield failing_method

    @orca.thread(labware=other, start="pad_2", end="pad_2")
    async def other_journey(ctx: ThreadContext):
        yield other_method

    @orca.workflow(name="default_policy")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(failing_journey)
        wf.start(other_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world)
        paused = await wait_for_a_pause(world, execution_id, template="failing")
        assert paused.pause_reason == "error"
        assert paused.paused_device_command == "shake"
        await wait_until(
            lambda: [t.status for t in world.runtime.list_threads(execution_id) if t.labware_template_name == "other"] == ["COMPLETED"],
            timeout=60, message=f"the other thread never finished: {statuses(world, execution_id)}",
        )

        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [100.0, 200.0, 200.0]
        assert speeds(world, "shaker_2") == [300.0]
    finally:
        await close_world(world)
