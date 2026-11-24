"""`thread(required=False)`: a group that names the thread runs it.

A group that leaves it out: `test_an_optional_thread_left_out.py`.
"""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    Lab,
    build_world,
    close_world,
    new_lab,
    plate_template,
    run_to_end,
    speeds,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _with_an_optional_control(lab: Lab) -> WorkflowTemplate:
    sample, control = plate_template("sample"), plate_template("control")

    @orca.action(device=lab.shaker_1, inputs=[sample])
    async def shake_sample(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.shaker_2, inputs=[control])
    async def shake_control(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.method
    async def sample_method(ctx: MethodContext):
        yield shake_sample

    @orca.method
    async def control_method(ctx: MethodContext):
        yield shake_control

    @orca.thread(labware=sample, start="pad_1", end="pad_1")
    async def sample_journey(ctx: ThreadContext):
        yield sample_method

    @orca.thread(labware=control, start="pad_2", end="pad_2", required=False)
    async def control_journey(ctx: ThreadContext):
        yield control_method

    @orca.workflow(name="optional_control")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.start(control_journey)

    return workflow


async def run_with(members: tuple[str, ...], control_shakes: list[float]) -> None:
    lab = new_lab()
    world = await build_world(lab, _with_an_optional_control(lab))
    try:
        group = LabwareGroup(id="g1", members=tuple(LabwareGroupMember(m) for m in members))
        execution_id, ended = await run_to_end(world, groups=[group])

        assert set(ended.values()) == {"COMPLETED"}, ended
        templates = sorted(t.labware_template_name or "" for t in world.runtime.list_threads(execution_id))
        assert templates == sorted(m.removesuffix("_journey") for m in members)
        assert speeds(world, "shaker_1") == [100.0]
        assert speeds(world, "shaker_2") == control_shakes
    finally:
        await close_world(world)


async def test_a_group_that_names_the_optional_thread_runs_it() -> None:
    await run_with(("sample_journey", "control_journey"), [200.0])
