"""`SHARED_ACROSS_GROUPS` feeds one receiver from every group; `BATCHABLE` + `JOIN_EXISTING` feeds the accepting run's receiver, and a submission after close gets its own."""

import asyncio

import pytest

from orca.resource_models.capacity import CapacityPolicy
from orca.state.identity import LabwareRef
from orca.state.records import InitialStateDetails
from orca.workflow_models.submission_modes import BatchMode
from tests.sdk_features.capacity_world import (
    TRANSFER_UL,
    Transfers,
    capacity_workflow,
    sample_group,
    sample_groups,
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

# Above the sum of the waits one test makes in turn, so a slow wait fails with its own message.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(360)]

ROOM = CapacityPolicy(max_contributions=8)


async def test_every_group_in_a_submission_feeds_one_receiver() -> None:
    lab = new_lab()
    transfers = Transfers()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=ROOM))
    try:
        execution_id = await submit(world, groups=sample_groups(3))
        ended = await wait_for_the_end(world, execution_id, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended

        [receiver_id] = labware_of(world, execution_id, "final")
        receiver = await plate_record(world, receiver_id)
        assert {r for _, r, _ in transfers.seen} == {receiver.name}
        assert sum(receiver.volumes.values()) == 3 * TRANSFER_UL
    finally:
        await close_world(world)


async def test_a_joining_submission_feeds_the_accepting_receiver_and_one_after_close_gets_its_own() -> None:
    lab = new_lab()
    transfers = Transfers()
    first_transfer = asyncio.Event()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=ROOM, hold_transfer=(0, first_transfer)))
    try:
        first = await submit(world, groups=[sample_group(1)])
        await wait_until(lambda: len(transfers.arrivals) == 1, timeout=60)
        joined = await submit(world, groups=[sample_group(2)], batch_mode=BatchMode.JOIN_EXISTING)
        assert joined == first
        first_transfer.set()
        world.runtime.close_execution(first)
        ended = await wait_for_the_end(world, first, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended
        [receiver_id] = labware_of(world, first, "final")
        receiver = await plate_record(world, receiver_id)
        assert sum(receiver.volumes.values()) == 2 * TRANSFER_UL

        later = await submit(world, groups=[sample_group(3)], batch_mode=BatchMode.JOIN_EXISTING)
        ended = await wait_for_the_end(world, later, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert later != first
        [later_receiver] = labware_of(world, later, "final")
        assert later_receiver != receiver_id
        assert sum((await plate_record(world, later_receiver)).volumes.values()) == TRANSFER_UL
        assert sum((await plate_record(world, receiver_id)).volumes.values()) == 2 * TRANSFER_UL
    finally:
        await close_world(world)


async def test_a_join_submitted_while_the_tip_rack_is_being_bound_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    lab = new_lab()
    transfers = Transfers()
    first_transfer = asyncio.Event()
    world = await build_world(lab, capacity_workflow(lab, transfers, capacity=ROOM, hold_transfer=(0, first_transfer)))
    ledger = world.build.system.labware_contents
    seed_at_birth = ledger.seed_at_birth
    writing_the_tips = asyncio.Event()
    finish_writing = asyncio.Event()

    async def held_seed_at_birth(labware: LabwareRef, declared: InitialStateDetails | None) -> None:
        if labware.name.startswith("tips"):
            writing_the_tips.set()
            await finish_writing.wait()
        await seed_at_birth(labware, declared)

    monkeypatch.setattr(ledger, "seed_at_birth", held_seed_at_birth)
    try:
        first = await submit(world, groups=[sample_group(1)])
        await asyncio.wait_for(writing_the_tips.wait(), timeout=60)
        joined = await submit(world, groups=[sample_group(2)], batch_mode=BatchMode.JOIN_EXISTING)
        assert joined == first
        finish_writing.set()
        first_transfer.set()
        world.runtime.close_execution(first)
        ended = await wait_for_the_end(world, first, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended
    finally:
        finish_writing.set()
        await close_world(world)
