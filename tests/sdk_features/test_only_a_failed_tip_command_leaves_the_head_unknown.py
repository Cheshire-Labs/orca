"""A failed tip command in a run leaves the head unsure; any other failed command leaves the head known and flags
only the labware it names."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler
from orca.spawn import DISPENSE
from orca.state.provenance import Provenance
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    PLATE_SITE,
    TIP_SITE,
    Lab,
    build_world,
    close_world,
    new_lab,
    plate_template,
    run_to_pause,
    tips_template,
    tips_thread,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _pick_then_aspirate(lab: Lab) -> WorkflowTemplate:
    plate = plate_template()
    tips = tips_template()

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE})
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

    @orca.workflow(name="tip_failures")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_thread(tips, [take_method]))

    return workflow


async def _paused_after_a_failed(failing: str) -> tuple[Provenance, Provenance]:
    """How sure the record is of the head, and of the plate, once a run pauses on a failed `failing` command."""
    lab = new_lab()
    lab.log("lh").failures[failing] = 1
    world = await build_world(lab, _pick_then_aspirate(lab))
    try:
        _, paused = await run_to_pause(world)
        assert paused.pause_reason == "error"
        assert paused.labware_id is not None
        head = await world.runtime.devices.get_mounted_tips("lh")
        volumes = await world.runtime.labware.get_well_volumes(paused.labware_id)
        return head.provenance, volumes.provenance
    finally:
        await close_world(world)


async def test_a_failed_pick_leaves_the_head_unsure() -> None:
    head, _ = await _paused_after_a_failed("pick_up_tips")
    assert head is not Provenance.KNOWN


async def test_a_failed_aspirate_flags_the_plate_and_leaves_the_head_known() -> None:
    head, plate = await _paused_after_a_failed("aspirate")
    assert head is Provenance.KNOWN
    assert plate is Provenance.STALE
