"""`thread(immovable=True)`: the labware never moves, and a thread that needs its location pauses with the deadlock error at once."""

import asyncio

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IProtocolRunner, IShaker
from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    PLATE_SITE,
    SECOND_PLATE_SITE,
    Hold,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    recover,
    submit,
    wait_for_a_pause,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

REAGENT_SITE = f"lh/{SECOND_PLATE_SITE}"


async def test_a_thread_needing_the_immovable_plates_site_pauses_at_once() -> None:
    lab = new_lab()
    sample_held, blocker_held = Hold(), Hold()
    lab.log("shaker_1").holds["shake"] = sample_held
    lab.log("shaker_2").holds["shake"] = blocker_held
    sample, reagent, blocker = plate_template("sample"), plate_template("reagent"), plate_template("blocker")

    @orca.action(device=lab.lh, inputs=[sample, reagent], deck_positions={sample: PLATE_SITE, reagent: SECOND_PLATE_SITE})
    async def use_reagent(ctx: ActionContext) -> None:
        await ctx.device(IProtocolRunner).run_protocol("Use.hsl", {})

    @orca.action(device=lab.shaker_1, inputs=[sample])
    async def incubate(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.shaker_2, inputs=[blocker])
    async def wait_its_turn(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.action(device=lab.lh, inputs=[blocker], deck_positions={blocker: SECOND_PLATE_SITE})
    async def wants_the_reagent_site(ctx: ActionContext) -> None:
        await ctx.device(IProtocolRunner).run_protocol("Blocked.hsl", {})

    @orca.method
    async def use_method(ctx: MethodContext):
        yield use_reagent
        yield incubate

    @orca.method
    async def blocker_method(ctx: MethodContext):
        yield wait_its_turn
        yield wants_the_reagent_site

    @orca.thread(labware=sample, start="pad_1", end="pad_1")
    async def sample_journey(ctx: ThreadContext):
        yield use_method

    @orca.thread(labware=blocker, start="pad_2", end="pad_2")
    async def blocker_journey(ctx: ThreadContext):
        yield blocker_method

    @orca.thread(labware=reagent, start=(REAGENT_SITE, REUSE_EXISTING), end=(REAGENT_SITE, LEAVE_IN_PLACE), immovable=True)
    async def reagent_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[use_method])

    @orca.workflow(name="immovable_reagent")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(sample_journey)
        wf.start(blocker_journey)
        wf.thread(reagent_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(sample_held.started.wait(), timeout=60)
        blocker_held.finish.set()

        paused = await wait_for_a_pause(world, execution_id, template="blocker", timeout=20)
        assert "UnresolvableDeadlock" in (paused.last_error or "") + (paused.pause_message or "")
        [sample] = [t for t in world.runtime.list_threads(execution_id) if t.labware_template_name == "sample"]
        assert sample.status == "EXECUTING_ACTION", "the pause came while the reagent was still in use, not after a wait"

        await recover(world, execution_id, paused.id, RecoveryDecision.ABORT_THREAD)
        sample_held.finish.set()
        await wait_for_the_end(world, execution_id)
        [reagent_id] = labware_of(world, execution_id, "reagent")
        assert set((await plate_record(world, reagent_id)).history) == {REAGENT_SITE}
    finally:
        await close_world(world)
