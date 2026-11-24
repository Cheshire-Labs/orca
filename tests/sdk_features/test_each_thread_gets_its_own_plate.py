"""With two plates of one template in a run, each thread's commands and record changes name its own plate."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    PLATE_SITE,
    TIP_SITE,
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    run_to_end,
    tips_template,
    tips_thread,
    wells_named,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_each_thread_aspirates_from_its_own_plate_instance() -> None:
    lab = new_lab()
    plate = plate_template()
    tips = tips_template()
    names_seen: list[str] = []

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE})
    async def take(ctx: ActionContext) -> None:
        names_seen.append(ctx.labware("plate").name)
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

    @orca.workflow(name="two_plates")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    world = await build_world(lab, workflow)
    try:
        groups = [LabwareGroup(id=f"g{i}", members=(LabwareGroupMember("plate_journey"),)) for i in (1, 2)]
        execution_id, ended = await run_to_end(world, groups=groups)
        assert set(ended.values()) == {"COMPLETED"}, ended

        plates = [await plate_record(world, plate_id) for plate_id in labware_of(world, execution_id, "plate")]
        assert len({p.labware_id for p in plates}) == 2
        aspirated = [wells_named(c) for c in commands(world, "lh", "aspirate")]
        assert sorted(aspirated) == sorted([[(p.name, "A1")] for p in plates])
        assert sorted(names_seen) == sorted(p.name for p in plates)
        for p in plates:
            assert p.volumes["A1"] == 95.0, f"{p.name} must lose exactly its own one aspirate"
    finally:
        await close_world(world)
