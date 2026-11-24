"""A custom `IOverflowStrategy`: `on_overflow` is called once per contributor over capacity, and where it routes them is where they go."""

import asyncio

import pytest

from orca.resource_models.capacity import CapacityPolicy
from orca.workflow_models.labware_state import IWorkflowRef, LabwareSlot
from orca.workflow_models.method import ExecutingMethod
from tests.sdk_features.capacity_world import (
    TRANSFER_UL,
    Transfers,
    capacity_workflow,
    sample_groups,
    seen_by_receiver,
)
from tests.sdk_features.kit import (
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    submit,
    wait_for_the_end,
)
from tests.test_helpers import wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


class _QueueForTheNextReceiver:
    """Routes each overflow to the receiver that follows the current one, and counts the calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod, workflow: IWorkflowRef) -> None:
        self.calls.append(method.id)
        slot.pending.append(method)


async def test_each_overflow_calls_the_strategy_once_and_goes_where_it_routes() -> None:
    lab = new_lab(second_handler=True)
    transfers = Transfers()
    strategy = _QueueForTheNextReceiver()
    receiver_full = asyncio.Event()
    workflow = capacity_workflow(
        lab, transfers, capacity=CapacityPolicy(max_contributions=2), overflow_strategy=strategy,
        hold_transfer=(1, receiver_full), on_either_handler=True,
    )
    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world, groups=sample_groups(3))
        await wait_until(lambda: len(strategy.calls) == 1, timeout=60, message="the strategy was never called")
        receiver_full.set()
        ended = await wait_for_the_end(world, execution_id, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended

        assert len(strategy.calls) == 1
        receivers = [await plate_record(world, i) for i in labware_of(world, execution_id, "final")]
        assert len(receivers) == 2
        served = [sample for sample, _, _ in transfers.seen]
        assert seen_by_receiver(transfers) == {receivers[0].name: served[:2], receivers[1].name: served[2:]}
        assert sum(receivers[1].volumes.values()) == TRANSFER_UL
    finally:
        await close_world(world)
