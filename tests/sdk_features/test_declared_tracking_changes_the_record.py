"""`action(declares=DeclaredTracking)`: after a closed protocol runs, the plate's stored record changes as declared."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IProtocolRunner
from orca.spawn import DISPENSE
from orca.state.records import DeclaredTracking, DeclaredVolumeTransfer
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.kit import (
    PLATE_SITE,
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    run_to_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_a_declared_transfer_moves_volume_on_the_record() -> None:
    lab = new_lab()
    plate = plate_template()
    declared = DeclaredTracking(volume_transferred=[DeclaredVolumeTransfer(
        source="plate", target="plate", volume_ul=25.0, source_wells=["A1"], target_wells=["B1"],
    )])

    @orca.action(device=lab.lh, inputs=[plate], deck_positions={plate: PLATE_SITE}, declares=declared)
    async def closed_protocol(ctx: ActionContext) -> None:
        await ctx.device(IProtocolRunner).run_protocol("Move25.hsl", {})

    @orca.method
    async def protocol_method(ctx: MethodContext):
        yield closed_protocol

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield protocol_method

    @orca.workflow(name="declared")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        [plate_id] = labware_of(world, execution_id, "plate")
        record = await plate_record(world, plate_id)
        assert record.volumes["A1"] == 75.0
        assert record.volumes["B1"] == 125.0
        assert record.volumes["C1"] == 100.0
        assert [c.method for c in commands(world, "lh") if c.method in ("aspirate", "dispense")] == []
    finally:
        await close_world(world)
