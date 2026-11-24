"""A thread, its events and its operation records name the labware group it serves; a thread shared across groups names none."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler
from orca.events.execution_context import ThreadExecutionContext
from orca.resource_models.sharing import GroupSharing
from orca.devices.devices import Storage
from orca.spawn import DISPENSE
from orca.state.records import DeviceOperation
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.real_device_path import the_real_device_path
from tests.sdk_features.kit import (
    PLATE_SITE,
    SECOND_PLATE_SITE,
    TIP_SITE,
    KitStorage,
    Lab,
    build_world,
    close_world,
    events,
    new_lab,
    operations,
    plate_template,
    run_to_end,
    tips_template,
    tips_thread,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

GROUPS = [LabwareGroup(id=g, members=(LabwareGroupMember("sample_journey"),)) for g in ("g1", "g2")]


def _lab_with_a_reagent_stacker() -> Lab:
    """A stacker holds one template, so the reagent has its own."""
    lab = new_lab(extra_teachpoints=["reagent_stacker"])
    driver = KitStorage("reagent_stacker")
    the_real_device_path().simulate_with("reagent_stacker", driver)
    lab.drivers["reagent_stacker"] = driver
    lab.more_locations["reagent_stacker"] = Storage("reagent_stacker")
    return lab


def _per_group_reagent(lab: Lab) -> WorkflowTemplate:
    sample = plate_template("sample", 0.0)
    reagent = plate_template("reagent", 100.0, group_sharing=GroupSharing.PER_GROUP)
    tips = tips_template(shared=True)

    @orca.action(
        device=lab.lh, inputs=[sample, reagent, tips],
        deck_positions={sample: PLATE_SITE, reagent: SECOND_PLATE_SITE, tips: TIP_SITE},
    )
    async def add_reagent(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 1))
        await lh.aspirate([ctx.plate("reagent").well("A1")], [5.0])
        await lh.dispense([ctx.plate("sample").well("A1")], [5.0])
        await lh.discard_tips()

    @orca.method
    async def add_method(ctx: MethodContext):
        yield add_reagent

    @orca.thread(labware=sample, start=("stacker", DISPENSE), end="waste")
    async def sample_journey(ctx: ThreadContext):
        yield add_method

    @orca.thread(labware=reagent, start=("reagent_stacker", DISPENSE), end="waste")
    async def reagent_journey(ctx: ThreadContext):
        yield orca.join(allows=[add_method])

    tips_journey = tips_thread(tips, [add_method])

    @orca.workflow(name="reagent_per_group")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.thread(reagent_journey)
        wf.thread(tips_journey)

    return workflow


async def test_each_groups_transfer_records_and_threads_name_that_group() -> None:
    lab = _lab_with_a_reagent_stacker()
    world = await build_world(lab, _per_group_reagent(lab))
    try:
        execution_id, ended = await run_to_end(world, groups=GROUPS, timeout=150)
        assert set(ended.values()) == {"COMPLETED"}, ended

        threads = world.runtime.list_threads(execution_id)
        group_of = {t.name: t.group_id for t in threads}
        by_template = {
            template: sorted(str(t.group_id) for t in threads if t.labware_template_name == template)
            for template in ("sample", "reagent")
        }
        assert by_template == {"sample": ["g1", "g2"], "reagent": ["g1", "g2"]}
        assert [t.group_id for t in threads if t.labware_template_name == "tips"] == [None]

        transfers: dict[str, list[tuple[DeviceOperation, str, str | None]]] = {}
        for record in await operations(world, execution_id):
            for op in record.operations:
                if op.device_name == "lh" and op.operation in (DeviceOperation.ASPIRATE, DeviceOperation.DISPENSE):
                    [labware] = op.affected_labware
                    transfers.setdefault(op.action_id, []).append((op.operation, labware, op.group_id))
        assert len(transfers) == 2, transfers
        served = set()
        for pair in transfers.values():
            [(_, reagent, aspirate_group), (_, sample, dispense_group)] = pair
            assert aspirate_group == dispense_group == group_of[reagent] == group_of[sample], pair
            served.add(aspirate_group)
        assert served == {"g1", "g2"}

        on_events: dict[str, set[str | None]] = {}
        for event in events(world, "THREAD."):
            assert isinstance(event.context, ThreadExecutionContext), event
            on_events.setdefault(event.context.thread_id, set()).add(event.context.group_id)
        assert on_events == {t.id: {t.group_id} for t in threads}
    finally:
        await close_world(world)
