"""`action(deck_positions=)`: each labware sits at its named deck slot during the action, and the device command names that slot."""

import asyncio

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    LH_ENTRY,
    SECOND_PLATE_SITE,
    TIP_SITE,
    TIPS_START,
    Hold,
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    submit,
    tips_template,
    tips_thread,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_plate_is_at_its_deck_slot_during_the_action_and_the_move_names_it() -> None:
    lab = new_lab()
    hold = Hold()
    lab.log("lh").holds["aspirate"] = hold
    plate = plate_template()
    tips = tips_template()

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: SECOND_PLATE_SITE, tips: TIP_SITE})
    async def take(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 1))
        await lh.aspirate([ctx.plate("plate").well("A1")], [5.0])
        await lh.discard_tips()

    @orca.method
    async def take_method(ctx: MethodContext):
        yield take

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield take_method

    tips_journey = tips_thread(tips, [take_method])

    @orca.workflow(name="deck_slot")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(hold.started.wait(), timeout=60)
        [plate_id] = labware_of(world, execution_id, "plate")
        [tips_id] = labware_of(world, execution_id, "tips")

        assert (await plate_record(world, plate_id)).location == f"lh/{SECOND_PLATE_SITE}"
        assert (await plate_record(world, tips_id)).location == TIPS_START
        plate_name = (await plate_record(world, plate_id)).name
        moves = [(c.args["plate"], c.args["to_position"]) for c in commands(world, "lh", "move_plate")]
        assert (plate_name, SECOND_PLATE_SITE) in moves

        hold.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert LH_ENTRY in (await plate_record(world, plate_id)).history
    finally:
        await close_world(world)
