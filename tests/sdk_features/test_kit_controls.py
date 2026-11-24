"""Each kit read sees what happened (positive) and does not see what did not (negative)."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import ILiquidHandler, IShaker
from orca.resource_models.labware import LabwareInitialState
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.sdk_features.kit import (
    PLATE_SITE,
    PLATE_TYPE,
    TIP_SITE,
    TIP_TYPE,
    TIPS_START,
    FeatureWorld,
    Lab,
    build_world,
    close_world,
    commands,
    events,
    labware_of,
    new_lab,
    operations,
    plate_record,
    recover,
    run_to_end,
    run_to_pause,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _shake_then_aspirate(lab: Lab) -> WorkflowTemplate:
    plate = PlateTemplate("plate", labware_type=PLATE_TYPE, initial_state=LabwareInitialState(uniform_volume=100.0))
    tips = TipRackTemplate("tips", labware_type=TIP_TYPE, with_tips=True)

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=500)

    @orca.action(device=lab.lh, inputs=[plate, tips], deck_positions={plate: PLATE_SITE, tips: TIP_SITE})
    async def take(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 1))
        await lh.aspirate([ctx.plate("plate").well("A1")], [5.0])
        await lh.discard_tips()

    @orca.method
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.method
    async def take_method(ctx: MethodContext):
        yield take

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield shake_method
        yield take_method

    @orca.thread(labware=tips, start=(TIPS_START, REUSE_EXISTING), end=(TIPS_START, LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[take_method])

    @orca.workflow(name="controls")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)
        wf.thread(tips_journey)

    return workflow


async def _ran() -> tuple[FeatureWorld, str]:
    lab = new_lab()
    world = await build_world(lab, _shake_then_aspirate(lab))
    execution_id, ended = await run_to_end(world)
    assert set(ended.values()) == {"COMPLETED"}, ended
    return world, execution_id


async def test_commands_lists_what_a_device_was_sent_and_nothing_for_an_idle_one() -> None:
    world, _ = await _ran()
    try:
        assert [(c.method, c.args["speed"]) for c in commands(world, "shaker_1")] == [("shake", 500.0)]
        assert commands(world, "shaker_2") == []
        assert [c.method for c in commands(world, "lh")] == [
            "move_plate", "pick_up_tips", "aspirate", "discard_tips", "move_plate",
        ]
        assert len(commands(world, "stacker", "dispense")) == 1
        assert commands(world, "waste") == []
    finally:
        await close_world(world)


async def test_plate_record_reads_the_stored_volumes_and_route_of_one_instance() -> None:
    world, execution_id = await _ran()
    try:
        [plate_id] = labware_of(world, execution_id, "plate")
        record = await plate_record(world, plate_id)
        assert record.volumes["A1"] == 95.0
        assert record.volumes["B1"] == 100.0
        assert record.history[0] == "stacker/slot"
        assert f"lh/{PLATE_SITE}" in record.history
        assert "shaker_2" not in record.history
    finally:
        await close_world(world)


async def test_events_holds_the_runs_events_and_none_it_did_not_emit() -> None:
    world, execution_id = await _ran()
    try:
        assert [e for e in events(world, "THREAD.") if e.execution_id == execution_id and e.status == "COMPLETED"]
        assert events(world, "CUSTOM.") == []
    finally:
        await close_world(world)


async def test_operations_holds_the_runs_records_and_none_for_another_run() -> None:
    world, execution_id = await _ran()
    try:
        devices = {op.device_name for record in await operations(world, execution_id) for op in record.operations}
        assert {"shaker_1", "lh", "arm"} <= devices
        assert "shaker_2" not in devices
        assert await operations(world, "no-such-run") == []
    finally:
        await close_world(world)


async def test_a_planned_failure_pauses_until_recovered() -> None:
    lab = new_lab()
    lab.log("shaker_1").failures["shake"] = 1
    world = await build_world(lab, _shake_then_aspirate(lab))
    try:
        execution_id, paused = await run_to_pause(world, template="plate")
        assert paused.pause_reason == "error"
        assert len(commands(world, "shaker_1", "shake")) == 1
        assert commands(world, "lh") == []

        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}
        assert len(commands(world, "shaker_1", "shake")) == 2
    finally:
        await close_world(world)
