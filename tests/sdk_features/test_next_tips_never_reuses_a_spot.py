"""`ctx.next_tips` hands out spots no pick has used, and the rack's record drops as soon as each pick returns."""

import pytest

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
    FeatureWorld,
    RecordedCommand,
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_template,
    run_to_end,
    tips_template,
    tips_thread,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _spots(command: RecordedCommand) -> list[str]:
    picks = command.args["picks"]
    assert isinstance(picks, list), command.args
    return [str(spot) for pick in picks if isinstance(pick, dict)
            for positions in [pick["positions"]] if isinstance(positions, list) for spot in positions]


async def test_two_picks_take_four_different_spots_and_the_record_drops_after_each() -> None:
    lab = new_lab()
    plate = plate_template()
    tips = tips_template()
    worlds: list[FeatureWorld] = []
    present_after_the_first_pick: list[int] = []

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE})
    async def two_picks(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        await lh.discard_tips()
        [world] = worlds
        rack = ctx.labware("tips").id
        present_after_the_first_pick.append(len((await world.runtime.labware.get_tip_state(rack)).positions_present))
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        await lh.discard_tips()

    @orca.method
    async def pick_method(ctx: MethodContext):
        yield two_picks

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield pick_method

    @orca.workflow(name="two_tip_picks")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_thread(tips, [pick_method]))

    world = await build_world(lab, workflow)
    worlds.append(world)
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        spots = [spot for c in commands(world, "lh", "pick_up_tips") for spot in _spots(c)]
        assert len(spots) == 4 and len(set(spots)) == 4, spots
        [rack] = labware_of(world, execution_id, "tips")
        assert present_after_the_first_pick == [94]
        assert len((await world.runtime.labware.get_tip_state(rack)).positions_present) == 92
    finally:
        await close_world(world)
