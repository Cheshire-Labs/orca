"""`ctx.pool_index` gives each contributor to a receiver 0, 1, ... in arrival order; a non-contributor that asks pauses."""

import pytest

import orca.orca as orca
from orca.devices.device_interfaces import IShaker
from orca.resource_models.capacity import CapacityPolicy
from orca.spawn import DISPENSE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.sdk_features.capacity_world import (
    TRANSFER_UL,
    WELLS,
    Transfers,
    capacity_workflow,
    sample_groups,
    transfers_by_receiver,
)
from tests.sdk_features.kit import (
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_record,
    plate_template,
    run_to_end,
    run_to_pause,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_each_contributor_lands_in_its_own_well_in_arrival_order() -> None:
    lab = new_lab()
    transfers = Transfers()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=CapacityPolicy(max_contributions=3)))
    try:
        execution_id, ended = await run_to_end(world, groups=sample_groups(3), timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended

        [receiver_id] = labware_of(world, execution_id, "final")
        receiver = await plate_record(world, receiver_id)
        assert [index for _, _, index in transfers.seen] == [0, 1, 2]
        assert transfers_by_receiver(world) == {receiver.name: [sample for sample, _, _ in transfers.seen]}
        assert [receiver.volumes[w] for w in WELLS[:3]] == [TRANSFER_UL] * 3
        assert sum(receiver.volumes.values()) == 3 * TRANSFER_UL
    finally:
        await close_world(world)


async def test_an_action_that_does_not_contribute_pauses_when_it_asks_for_an_index() -> None:
    lab = new_lab()
    plate = plate_template()

    @orca.action(device=lab.shaker_1, inputs=[plate])
    async def ask(ctx: ActionContext) -> None:
        ctx.pool_index("final")
        await ctx.device(IShaker).shake(duration=1, speed=100)

    @orca.method
    async def ask_method(ctx: MethodContext):
        yield ask

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield ask_method

    @orca.workflow(name="asks_for_an_index")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(plate_journey)

    world = await build_world(lab, workflow)
    try:
        _, paused = await run_to_pause(world)

        assert paused.pause_reason == "error"
        assert "does not contribute to receiver 'final'" in (paused.last_error or "") + (paused.pause_message or "")
        assert commands(world, "shaker_1") == []
    finally:
        await close_world(world)
