"""`can_continue_fn`: a receiver whose function says it cannot take more is replaced, even below its capacity."""

import pytest

from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware import LabwareInstance
from orca.state.records import DeclaredTracking
from tests.sdk_features.capacity_world import Transfers, capacity_workflow, sample_groups
from tests.sdk_features.kit import build_world, close_world, labware_of, new_lab, run_to_end

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_each_contribution_gets_its_own_receiver_when_the_first_cannot_continue() -> None:
    asked: list[str] = []

    async def one_contribution_only(receiver: LabwareInstance, declared: DeclaredTracking | None) -> bool:
        asked.append(receiver.name)
        return False

    lab = new_lab()
    transfers = Transfers()
    world = await build_world(lab, capacity_workflow(
        lab, transfers, capacity=CapacityPolicy(max_contributions=3), receiver_can_continue=one_contribution_only,
    ))
    try:
        execution_id, ended = await run_to_end(world, groups=sample_groups(2), timeout=120)

        assert set(ended.values()) == {"COMPLETED"}, ended
        receivers = labware_of(world, execution_id, "final")
        assert len(receivers) == 2
        assert len({receiver for _, receiver, _ in transfers.seen}) == 2
        assert asked, "the function was asked"
    finally:
        await close_world(world)
