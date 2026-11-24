"""Overflow `NEW`, the default: once a receiver is full, the next contributor gets a new receiver while the full one still runs."""

import pytest

from orca.resource_models.capacity import CapacityPolicy
from tests.sdk_features.capacity_world import Transfers, capacity_workflow, sample_groups
from tests.sdk_features.kit import (
    Hold,
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

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_the_second_receiver_takes_a_contribution_while_the_first_is_still_shaking() -> None:
    lab = new_lab()
    hold = Hold()
    lab.log("shaker_2").holds["shake"] = hold
    transfers = Transfers()
    world = await build_world(lab, capacity_workflow(
        lab, transfers, capacity=CapacityPolicy(max_contributions=1), receiver_shakes_when_full=True,
    ))
    try:
        execution_id = await submit(world, groups=sample_groups(2))
        await hold.started.wait()
        await wait_until(lambda: len(transfers.seen) == 2, timeout=60, message="the second contribution never ran")

        [first, second] = [t for t in world.runtime.list_threads(execution_id) if t.labware_template_name == "final"]
        assert first.status == "EXECUTING_ACTION", "the full receiver is still shaking"
        assert [receiver for _, receiver, _ in transfers.seen] == [first.name, second.name]
        assert not [e for e in events(world, "SLOT.") if e.execution_id == execution_id and e.status in ("OVERFLOW", "REJECTED")]

        del lab.log("shaker_2").holds["shake"]
        hold.finish.set()
        ended = await wait_for_the_end(world, execution_id, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended
        for receiver in labware_of(world, execution_id, "final"):
            assert sum((await plate_record(world, receiver)).volumes.values()) > 0
    finally:
        await close_world(world)
