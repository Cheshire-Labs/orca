"""`ctx.has_more_work()`: a receiver stays open while a feeder two links up its `contributes_to` chain is live, and ends after."""

import asyncio

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IProtocolRunner, IShaker
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.spawn import DISPENSE
from orca.state.records import DeclaredTracking, DeclaredVolumeTransfer
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    PLATE_SITE,
    SECOND_PLATE_SITE,
    SHELVES,
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    speeds,
    submit,
    wait_for_the_end,
)
from tests.test_helpers import let_the_loop_run, wait_until

MID_SITE = "carrier-7-3"

# Above the sum of the waits one test makes in turn, so a slow wait fails with its own message.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(270)]


def _moves(source: str, target: str) -> DeclaredTracking:
    return DeclaredTracking(volume_transferred=[DeclaredVolumeTransfer(
        source=source, target=target, volume_ul=10.0, source_wells=["A1"], target_wells=["A1"],
    )])


async def test_the_receiver_waits_for_a_live_feeder_two_links_up_then_finishes() -> None:
    lab = new_lab()
    second_feeder_may_go = asyncio.Event()
    feeders_seen: list[str] = []
    feeder = plate_template("feeder")
    mid = plate_template("mid", 0.0)
    final = plate_template(
        "final", 0.0, group_sharing=GroupSharing.SHARED_ACROSS_GROUPS, submission_batching=SubmissionBatching.BATCHABLE,
    )

    @orca.action(device=lab.shaker_1, inputs=[feeder])
    async def prepare(ctx: ActionContext) -> None:
        feeders_seen.append(ctx.labware("feeder").name)
        if len(feeders_seen) == 2:
            await second_feeder_may_go.wait()
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.lh, inputs=[feeder, mid], deck_positions={feeder: PLATE_SITE, mid: MID_SITE},
                 declares=_moves("feeder", "mid"))
    async def make_mid(ctx: ActionContext) -> None:
        await ctx.device(IProtocolRunner).run_protocol("MakeMid.hsl", {})

    @orca.action(device=lab.lh, inputs=[mid, final], deck_positions={mid: MID_SITE, final: SECOND_PLATE_SITE},
                 declares=_moves("mid", "final"))
    async def use_final(ctx: ActionContext) -> None:
        await ctx.device(IProtocolRunner).run_protocol("UseFinal.hsl", {})

    @orca.action(device=lab.shaker_2, inputs=[final])
    async def finish(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=900)

    @orca.method
    async def prepare_method(ctx: MethodContext):
        yield prepare

    @orca.method
    async def make_mid_method(ctx: MethodContext):
        yield make_mid

    @orca.method
    async def use_final_method(ctx: MethodContext):
        yield use_final

    @orca.method
    async def finish_method(ctx: MethodContext):
        yield finish

    @orca.thread(labware=feeder, start=("stacker", DISPENSE), end="waste", contributes_to=["mid"])
    async def feeder_journey(ctx: ThreadContext):
        yield prepare_method
        yield make_mid_method

    @orca.thread(labware=mid, start=SHELVES[0], end="waste", contributes_to=["final"])
    async def mid_journey(ctx: ThreadContext):
        yield orca.join(allows=[make_mid_method])
        yield use_final_method

    @orca.thread(labware=final, start=SHELVES[1], end="pad_2")
    async def final_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[use_final_method])
        yield finish_method

    @orca.workflow(name="chain")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(feeder_journey)
        wf.thread(mid_journey)
        wf.thread(final_journey)

    world = await build_world(lab, workflow)
    try:
        groups = [LabwareGroup(id=f"g{i}", members=(LabwareGroupMember("feeder_journey"),)) for i in (1, 2)]
        execution_id = await submit(world, groups=groups)
        await wait_until(lambda: len(commands(world, "lh", "run_protocol")) == 2 and len(feeders_seen) == 2, timeout=90)
        await let_the_loop_run()
        assert speeds(world, "shaker_2") == [], "the receiver is still open while the second feeder is live"

        second_feeder_may_go.set()
        ended = await wait_for_the_end(world, execution_id, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_2") == [900.0]
        [final_id] = labware_of(world, execution_id, "final")
        assert (await plate_record(world, final_id)).volumes["A1"] == 20.0
        last_use = max(c.started for c in commands(world, "lh", "run_protocol"))
        assert commands(world, "shaker_2")[0].started > last_use
    finally:
        await close_world(world)
