"""`CapacityPolicy(max_contributions=N)`: exactly N contributors feed one receiver, and contributor N+1 gets a new one."""

import pytest

from orca.resource_models.capacity import CapacityPolicy
from tests.sdk_features.capacity_world import (
    TRANSFER_UL,
    Transfers,
    capacity_workflow,
    sample_groups,
    transfers_by_receiver,
)
from tests.sdk_features.kit import (
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    run_to_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_two_contributions_fill_a_receiver_and_the_third_gets_a_new_one() -> None:
    lab = new_lab()
    transfers = Transfers()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=CapacityPolicy(max_contributions=2)))
    try:
        execution_id, ended = await run_to_end(world, groups=sample_groups(3), timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended

        receivers = [await plate_record(world, i) for i in labware_of(world, execution_id, "final")]
        samples = {r.name: r for r in [await plate_record(world, i) for i in labware_of(world, execution_id, "sample")]}
        assert len({r.labware_id for r in receivers}) == 2
        by_receiver = transfers_by_receiver(world)
        first, second = receivers
        arrival = [sample for sample, _, _ in transfers.seen]
        assert by_receiver == {first.name: arrival[:2], second.name: arrival[2:]}
        assert first.volumes["A1"] == first.volumes["B1"] == TRANSFER_UL
        assert sum(first.volumes.values()) == 2 * TRANSFER_UL
        assert second.volumes["A1"] == TRANSFER_UL
        assert sum(second.volumes.values()) == TRANSFER_UL
        assert all(s.volumes["A1"] == 100.0 - TRANSFER_UL for s in samples.values())
    finally:
        await close_world(world)
