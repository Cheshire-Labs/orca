"""`ctx.get_well_selector` returns the selector given in `action(well_selectors=)`, or all wells when none is given."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler
from orca.resource_models.well_selector import WellSelector, all_wells, well_list
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
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

ALL_COLUMN_1 = ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]


def _wells(selector: WellSelector) -> list[str]:
    """The wells an author takes from a selector: a list names its wells; `all` means column 1 here."""
    if selector.mode == "list" and isinstance(selector.spec, list):
        return selector.spec
    assert selector == all_wells()
    return ALL_COLUMN_1


def test_an_action_keeps_the_selector_it_was_given() -> None:
    lab = new_lab()
    plate = plate_template()

    @orca.action(device=lab.lh, inputs=[plate], well_selectors={"plate": well_list(["A1", "C1"])})
    async def take(ctx: ActionContext) -> None:
        return None

    assert take.well_selectors == {"plate": well_list(["A1", "C1"])}


async def test_the_commands_touch_only_the_selected_wells_and_no_selector_means_all() -> None:
    lab = new_lab()
    plate = plate_template()
    tips = tips_template()
    seen: list[WellSelector] = []

    async def take_from_selected(ctx: ActionContext) -> None:
        selector = ctx.get_well_selector("plate")
        seen.append(selector)
        wells = _wells(selector)
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", len(wells)))
        await lh.aspirate([ctx.plate("plate").well(w) for w in wells], [5.0] * len(wells))
        await lh.discard_tips()

    selected = orca.action(
        device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE},
        well_selectors={"plate": well_list(["A1", "C1"])},
    )(take_from_selected)
    unselected = orca.action(
        device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE},
    )(take_from_selected)

    @orca.method
    async def take_method(ctx: MethodContext):
        yield selected
        yield unselected

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield take_method

    tips_journey = tips_thread(tips, [take_method])

    @orca.workflow(name="selected_wells")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        assert seen == [well_list(["A1", "C1"]), all_wells()]
        [plate_id] = labware_of(world, execution_id, "plate")
        record = await plate_record(world, plate_id)
        first, second = commands(world, "lh", "aspirate")
        assert [w for _, w in wells_named(first)] == ["A1", "C1"]
        assert [w for _, w in wells_named(second)] == ALL_COLUMN_1
        assert record.volumes["A1"] == 90.0 and record.volumes["C1"] == 90.0
        assert record.volumes["B1"] == 95.0, "a well the selector left out is touched only by the unselected action"
        assert record.volumes["A2"] == 100.0
    finally:
        await close_world(world)
