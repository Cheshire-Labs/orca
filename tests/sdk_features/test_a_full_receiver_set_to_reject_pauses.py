"""`REJECT`: contributor N+1 pauses while the receiver is full, the others finish, RETRY completes it, ABORT_THREAD ends only it and the run is PARTIAL_COMPLETED."""

import asyncio

import pytest

from orca.resource_models.capacity import CapacityPolicy, OverflowAction
from orca.runtime.system_runtime import ExecutionState
from orca.workflow_models.status_enums import RecoveryDecision
from tests.sdk_features.capacity_world import (
    TRANSFER_UL,
    Transfers,
    capacity_workflow,
    sample_groups,
)
from tests.sdk_features.kit import (
    FeatureWorld,
    build_world,
    close_world,
    events,
    labware_of,
    new_lab,
    plate_record,
    recover,
    statuses,
    submit,
    wait_for_a_pause,
    wait_for_the_end,
)
from tests.test_helpers import wait_until

# Above the sum of the waits one test makes in turn, so a slow wait fails with its own message.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(390)]

POLICY = CapacityPolicy(max_contributions=2, overflow_action=OverflowAction.REJECT)


async def _third_paused(world: FeatureWorld, receiver_full: asyncio.Event) -> tuple[str, str]:
    """The third contributor arrives while the second transfer still holds the full receiver."""
    execution_id = await submit(world, groups=sample_groups(3))
    paused = await wait_for_a_pause(world, execution_id, template="sample", timeout=60)
    assert paused.pause_reason == "error"
    receiver_full.set()

    def others_done() -> bool:
        return sum(1 for t in world.runtime.list_threads(execution_id) if t.status == "COMPLETED"
                   and t.labware_template_name == "sample") == 2

    await wait_until(others_done, timeout=120, message=f"the other contributors never finished: {statuses(world, execution_id)}")
    rejected = [e for e in events(world, "SLOT.") if e.execution_id == execution_id and e.status == "REJECTED"]
    assert rejected, "a rejected contribution is announced"
    return execution_id, paused.id


async def test_retry_after_the_receiver_frees_lets_the_third_finish() -> None:
    lab = new_lab(second_handler=True)
    transfers = Transfers()
    receiver_full = asyncio.Event()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=POLICY, hold_transfer=(1, receiver_full), on_either_handler=True))
    try:
        execution_id, thread_id = await _third_paused(world, receiver_full)
        receivers = labware_of(world, execution_id, "final")
        first = await plate_record(world, receivers[0])
        assert sum(first.volumes.values()) == 2 * TRANSFER_UL

        await recover(world, execution_id, thread_id, RecoveryDecision.RETRY)
        ended = await wait_for_the_end(world, execution_id, timeout=120)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert len(transfers.seen) == 3
        assert sum((await plate_record(world, receivers[0])).volumes.values()) == 2 * TRANSFER_UL
    finally:
        await close_world(world)


async def test_abort_thread_ends_only_the_rejected_contributor() -> None:
    lab = new_lab(second_handler=True)
    transfers = Transfers()
    receiver_full = asyncio.Event()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=POLICY, hold_transfer=(1, receiver_full), on_either_handler=True))
    try:
        execution_id, thread_id = await _third_paused(world, receiver_full)

        await recover(world, execution_id, thread_id, RecoveryDecision.ABORT_THREAD)
        ended = await wait_for_the_end(world, execution_id, timeout=120)

        aborted = [t for t in world.runtime.list_threads(execution_id) if t.status == "ABORTED"]
        assert [t.id for t in aborted] == [thread_id]
        assert all(status == "COMPLETED" for name, status in ended.items() if name != aborted[0].name)
        assert len(transfers.seen) == 2
        assert (await asyncio.wait_for(world.runtime.wait(execution_id), timeout=30)).status is ExecutionState.PARTIAL_COMPLETED
    finally:
        await close_world(world)
