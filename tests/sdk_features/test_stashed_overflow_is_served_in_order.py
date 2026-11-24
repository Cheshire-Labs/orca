"""`SequentialStashStrategy`: an overflowing contributor waits, `SLOT.<key>.OVERFLOW` is emitted, and a new receiver serves it.

Service in arrival order: `test_stashed_contributors_are_served_in_arrival_order.py`.
"""

import asyncio

import pytest

from orca.resource_models.capacity import CapacityPolicy
from orca.workflow_models.overflow_strategy import SequentialStashStrategy
from orca.workflow_models.submission_modes import BatchMode
from tests.sdk_features.capacity_world import (
    TRANSFER_UL,
    Transfers,
    capacity_workflow,
    sample_group,
    seen_by_receiver,
)
from tests.sdk_features.kit import (
    FeatureWorld,
    build_world,
    close_world,
    events,
    labware_of,
    new_lab,
    plate_record,
    submit,
    wait_for_the_end,
)
from tests.test_helpers import wait_until

# Above the sum of the waits one test makes in turn, so a slow wait fails with its own message.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(360)]


def _overflows(world: FeatureWorld, execution_id: str) -> int:
    return len([e for e in events(world, "SLOT.") if e.execution_id == execution_id and e.status == "OVERFLOW"])


async def three_contributors_one_stashed() -> tuple[FeatureWorld, Transfers, str]:
    """Contributor 2 overflows while contributor 1 holds the receiver; contributor 3 arrives after it."""
    lab = new_lab(second_handler=True)
    transfers = Transfers()
    receiver_full = asyncio.Event()
    workflow = capacity_workflow(
        lab, transfers, capacity=CapacityPolicy(max_contributions=1), overflow_strategy=SequentialStashStrategy(),
        hold_transfer=(0, receiver_full), on_either_handler=True,
    )
    world = await build_world(lab, workflow)
    try:
        execution_id = await submit(world, groups=[sample_group(1)])
        await wait_until(lambda: len(transfers.arrivals) == 1, timeout=60)
        await submit(world, groups=[sample_group(2)], batch_mode=BatchMode.JOIN_EXISTING)
        await wait_until(lambda: _overflows(world, execution_id) == 1, timeout=60, message="the second contributor never overflowed")
        await submit(world, groups=[sample_group(3)], batch_mode=BatchMode.JOIN_EXISTING)
        await wait_until(lambda: len(transfers.arrivals) == 3, timeout=60)

        receiver_full.set()
        ended = await wait_for_the_end(world, execution_id, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended
        return world, transfers, execution_id
    except BaseException:
        await close_world(world)
        raise


async def test_an_overflowing_contributor_waits_and_a_new_receiver_serves_it() -> None:
    world, transfers, execution_id = await three_contributors_one_stashed()
    try:
        receivers = [await plate_record(world, i) for i in labware_of(world, execution_id, "final")]
        assert len(receivers) == 3
        assert _overflows(world, execution_id) >= 1
        assert sorted(s for s, _, _ in transfers.seen) == sorted(transfers.arrivals)
        assert all(len(samples) == 1 for samples in seen_by_receiver(transfers).values())
        assert all(sum(r.volumes.values()) == TRANSFER_UL for r in receivers)
    finally:
        await close_world(world)
