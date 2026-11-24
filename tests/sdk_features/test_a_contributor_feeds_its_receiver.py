"""`thread(contributes_to=[receiver])`: the contributor's transfers land on the receiver's record.

The receiver staying open while a feeder is live is in `test_a_receiver_loop_ends_when_no_feeder_is_live.py`.
"""

import pytest

from orca.resource_models.capacity import CapacityPolicy
from tests.sdk_features.capacity_world import (
    TRANSFER_UL,
    Transfers,
    capacity_workflow,
    sample_groups,
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


async def test_the_receiver_gains_what_each_contributor_lost() -> None:
    lab = new_lab()
    transfers = Transfers()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=CapacityPolicy(max_contributions=4)))
    try:
        execution_id, ended = await run_to_end(world, groups=sample_groups(2), timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended

        samples = [await plate_record(world, i) for i in labware_of(world, execution_id, "sample")]
        [receiver] = [await plate_record(world, i) for i in labware_of(world, execution_id, "final")]
        lost = sum(96 * 100.0 - sum(s.volumes.values()) for s in samples)
        assert lost == 2 * TRANSFER_UL
        assert sum(receiver.volumes.values()) == lost
    finally:
        await close_world(world)
