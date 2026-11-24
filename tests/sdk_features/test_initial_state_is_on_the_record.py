"""`initial_state=`: each plate's record starts with the declared volumes before any step runs."""

import asyncio

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.resource_models.labware import LabwareInitialState
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    PLATE_TYPE,
    Hold,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    submit,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_declared_volumes_are_on_the_record_before_the_first_command() -> None:
    lab = new_lab()
    held = Hold()
    lab.log("shaker_1").holds["shake"] = held
    plate = PlateTemplate(
        "plate", labware_type=PLATE_TYPE, initial_state=LabwareInitialState(wells={"A1": 50.0, "B2": 25.0}),
    )

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.method
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield shake_method

    @orca.workflow(name="seeded")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(held.started.wait(), timeout=60)
        [plate_id] = labware_of(world, execution_id, "plate")
        volumes = (await plate_record(world, plate_id)).volumes
        assert volumes["A1"] == 50.0 and volumes["B2"] == 25.0
        assert sum(volumes.values()) == 75.0

        held.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
    finally:
        await close_world(world)
