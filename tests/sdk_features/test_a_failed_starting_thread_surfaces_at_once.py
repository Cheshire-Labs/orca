"""An entry thread that fails at its start pauses at once, while its sibling entry threads still run."""

import asyncio

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.crash_world import crash_when_starting
from tests.sdk_features.kit import (
    Hold,
    build_world,
    close_world,
    new_lab,
    plate_template,
    submit,
    wait_for_a_pause,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_failed_entry_thread_pauses_while_its_sibling_is_still_working(monkeypatch: pytest.MonkeyPatch) -> None:
    crash_when_starting(monkeypatch, "broken")
    lab = new_lab()
    held = Hold()
    lab.log("shaker_1").holds["shake"] = held
    working, broken = plate_template("working"), plate_template("broken")

    @orca.action(device=lab.shaker_1, inputs=[working])
    async def long_shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.action(device=lab.shaker_2, inputs=[broken])
    async def never_runs(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=200)

    @orca.method
    async def working_method(ctx: MethodContext):
        yield long_shake

    @orca.method
    async def broken_method(ctx: MethodContext):
        yield never_runs

    @orca.thread(labware=working, start="pad_1", end="pad_1")
    async def working_journey(ctx: ThreadContext):
        yield working_method

    @orca.thread(labware=broken, start="pad_2", end="pad_2")
    async def broken_journey(ctx: ThreadContext):
        yield broken_method

    @orca.workflow(name="two_entries")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(working_journey)
        wf.start(broken_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(held.started.wait(), timeout=60)
        paused = await wait_for_a_pause(world, execution_id, template="broken", timeout=20)

        [sibling] = [t for t in world.runtime.list_threads(execution_id) if t.labware_template_name == "working"]
        assert sibling.status == "EXECUTING_ACTION", "the pause came while the sibling still works, not after it"
        assert "crash while starting broken" in (paused.last_error or "") + (paused.pause_message or "")
    finally:
        await close_world(world)
