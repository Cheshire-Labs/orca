"""`orca.park([spots])`: concurrent plates park on different spots, and a parked plate does not move until the engine wakes its thread."""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    Hold,
    Lab,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    submit,
    wait_for_the_end,
)
from tests.test_helpers import let_the_loop_run, wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

SPOTS = ["pad_1", "pad_2"]


def _parks_then_shakes(lab: Lab, name: str) -> ThreadTemplate:
    """Once the shaker is busy, the plate parks on either spot, then waits there for the shaker."""
    plate = plate_template(name)

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=200)

    async def shake_it(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    shake_method = MethodTemplate(f"shake_{name}", func=shake_it)

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def journey(ctx: ThreadContext):
        yield orca.on("shaker_busy")
        yield orca.park(SPOTS)
        yield shake_method

    return journey


async def test_two_plates_park_on_two_spots_and_wait_there() -> None:
    lab = new_lab()
    held = Hold()
    lab.log("shaker_1").holds["shake"] = held
    blocker = plate_template("blocker")

    @orca.action(device=lab.shaker_1, inputs=[blocker])
    async def occupy(ctx: ActionContext) -> None:
        await ctx.emit("shaker_busy")
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.method
    async def occupy_method(ctx: MethodContext):
        yield occupy

    @orca.thread(labware=blocker, start="hotel/shelf_3", end="hotel/shelf_3")
    async def blocker_journey(ctx: ThreadContext):
        yield occupy_method

    parkers = [_parks_then_shakes(lab, name) for name in ("plate_a", "plate_b")]

    @orca.workflow(name="parking")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(blocker_journey)
        for parker in parkers:
            wf.start(parker)

    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(held.started.wait(), timeout=60)
        plate_ids = labware_of(world, execution_id, "plate_a") + labware_of(world, execution_id, "plate_b")

        async def where() -> list[str | None]:
            return [(await plate_record(world, i)).location for i in plate_ids]

        async def both_parked() -> bool:
            return sorted(str(w) for w in await where()) == SPOTS

        await wait_until(both_parked, timeout=30)
        await let_the_loop_run()
        assert sorted(str(w) for w in await where()) == SPOTS, "neither parked plate moves while the shaker is busy"

        del lab.log("shaker_1").holds["shake"]
        held.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
    finally:
        await close_world(world)
