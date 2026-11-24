"""Aspirate and dispense carry the pipetting profile's height, flow rate and blow-out; a per-step patch overrides only the fields it names."""

import pytest
from cheshire_drivers.pipetting import PipettingProfile
from pydantic import JsonValue

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    PLATE_SITE,
    TIP_SITE,
    RecordedCommand,
    build_world,
    close_world,
    commands,
    new_lab,
    plate_template,
    run_to_end,
    tips_template,
    tips_thread,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

GLYCEROL = PipettingProfile(name="glycerol", height=2.0, flow_rate=20.0, blow_out=True, blow_out_volume=10.0)
SLOW_DRAW = PipettingProfile(name="slow draw", flow_rate=5.0)


def _parameters(command: RecordedCommand) -> dict[str, JsonValue]:
    found = command.args["parameters"]
    assert isinstance(found, dict)
    return {k: found[k] for k in ("height", "flow_rate", "blow_out", "blow_out_volume")}


async def test_the_profile_reaches_both_commands_and_a_step_patch_changes_only_its_field() -> None:
    lab = new_lab()
    plate = plate_template()
    tips = tips_template()

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE})
    async def move_glycerol(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 1))
        await lh.aspirate([ctx.plate("plate").well("A1")], [10.0], liquid_class=GLYCEROL, technique=SLOW_DRAW)
        await lh.dispense([ctx.plate("plate").well("B1")], [10.0], liquid_class=GLYCEROL)
        await lh.discard_tips()

    @orca.method
    async def glycerol_method(ctx: MethodContext):
        yield move_glycerol

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield glycerol_method

    tips_journey = tips_thread(tips, [glycerol_method])

    @orca.workflow(name="glycerol")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    world = await build_world(lab, workflow)
    try:
        _, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        [aspirate] = commands(world, "lh", "aspirate")
        [dispense] = commands(world, "lh", "dispense")
        assert _parameters(aspirate) == {"height": 2.0, "flow_rate": 5.0, "blow_out": True, "blow_out_volume": 10.0}
        assert _parameters(dispense) == {"height": 2.0, "flow_rate": 20.0, "blow_out": True, "blow_out_volume": 10.0}
    finally:
        await close_world(world)
