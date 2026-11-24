"""`DeckGripperTransporter`: between pick and place the plate is recorded in the liquid handler's gripper; after the place, at the new slot."""

import asyncio

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IProtocolRunner
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    LH_ENTRY,
    PLATE_SITE,
    Hold,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    submit,
    wait_for_the_end,
)

HANDLERS_GRIPPER = "lh/gripper"

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_plate_is_in_the_handlers_gripper_until_the_deck_move_places_it() -> None:
    lab = new_lab()
    held = Hold()
    lab.log("lh").holds["move_plate"] = held
    plate = plate_template()

    @orca.action(device=lab.lh, inputs=[plate], deck_positions={plate: PLATE_SITE})
    async def protocol(ctx: ActionContext) -> None:
        await ctx.device(IProtocolRunner).run_protocol("Read.hsl", {})

    @orca.method
    async def protocol_method(ctx: MethodContext):
        yield protocol

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield protocol_method

    @orca.workflow(name="deck_hop")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(held.started.wait(), timeout=60)
        [plate_id] = labware_of(world, execution_id, "plate")
        location = (await plate_record(world, plate_id)).location or ""
        assert location.startswith(HANDLERS_GRIPPER), location

        del lab.log("lh").holds["move_plate"]
        held.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        history = (await plate_record(world, plate_id)).history
        hop = history.index(LH_ENTRY)
        assert history[hop + 1].startswith(HANDLERS_GRIPPER)
        assert history[hop + 2] == f"lh/{PLATE_SITE}"
    finally:
        await close_world(world)
