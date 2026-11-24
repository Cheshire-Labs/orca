"""`orca.join(allows=)`: a joining thread shares the method, which runs once for both; a join against a method not in `allows` pauses."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IProtocolRunner, IShaker
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    PLATE_SITE,
    SECOND_PLATE_SITE,
    Lab,
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    run_to_end,
    run_to_pause,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _combine(lab: Lab, *, join_allows_combine: bool) -> WorkflowTemplate:
    first, second = plate_template("first"), plate_template("second")

    @orca.action(device=lab.lh, inputs=[first, second], deck_positions={first: PLATE_SITE, second: SECOND_PLATE_SITE})
    async def combine(ctx: ActionContext) -> None:
        await ctx.device(IProtocolRunner).run_protocol("Combine.hsl", {})

    @orca.action(device=lab.shaker_1, inputs=[second])
    async def unrelated(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.method
    async def combine_method(ctx: MethodContext):
        yield combine

    @orca.method
    async def other_method(ctx: MethodContext):
        yield unrelated

    @orca.thread(labware=first, start="pad_1", end="pad_1")
    async def first_journey(ctx: ThreadContext):
        yield combine_method

    @orca.thread(labware=second, start=("stacker", DISPENSE), end="waste")
    async def second_journey(ctx: ThreadContext):
        yield orca.join(allows=[combine_method if join_allows_combine else other_method])

    @orca.workflow(name="combine")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(first_journey)
        wf.thread(second_journey)

    return workflow


async def test_both_threads_share_one_run_of_the_method() -> None:
    lab = new_lab()
    world = await build_world(lab, _combine(lab, join_allows_combine=True))
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        assert [c.method for c in commands(world, "lh", "run_protocol")] == ["run_protocol"]
        [second_id] = labware_of(world, execution_id, "second")
        assert f"lh/{SECOND_PLATE_SITE}" in (await plate_record(world, second_id)).history
    finally:
        await close_world(world)


async def test_a_join_against_a_method_it_does_not_allow_pauses() -> None:
    lab = new_lab()
    world = await build_world(lab, _combine(lab, join_allows_combine=False))
    try:
        _, paused = await run_to_pause(world, template="second")

        assert paused.pause_reason == "error"
        assert "combine_method" in (paused.last_error or "") + (paused.pause_message or "")
        assert commands(world, "lh", "run_protocol") == []
        assert commands(world, "shaker_1") == []
    finally:
        await close_world(world)
