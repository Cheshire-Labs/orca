"""A joining submission is not refused over a deck-resident tip rack the run it joins already holds.

That rack was checked when the run was submitted, or the run is still minting it, so its tips are about to be
stated. Refusing the join in that window made a join sent right after the first submission fail.
"""

import asyncio

import pytest

from orca.resource_models.capacity import CapacityPolicy
from orca.resource_models.labware import LabwareInstance, TipRackInstance
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.submission_modes import BatchMode
from tests.sdk_features.capacity_world import Transfers, capacity_workflow, sample_group
from tests.sdk_features.kit import build_world, close_world, labware_of, new_lab, submit, wait_for_the_end
from tests.test_helpers import wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_a_join_while_the_run_holds_a_rack_with_no_stated_tips_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def every_rack_is_unknown(self: SystemRuntime, labware: LabwareInstance) -> bool:
        return isinstance(labware, TipRackInstance)

    lab = new_lab()
    transfers = Transfers()
    first_transfer = asyncio.Event()
    world = await build_world(lab, capacity_workflow(
        lab, transfers, capacity=CapacityPolicy(max_contributions=8), hold_transfer=(0, first_transfer),
    ))
    try:
        first = await submit(world, groups=[sample_group(1)])
        await wait_until(lambda: bool(labware_of(world, first, "tips")), timeout=60)
        monkeypatch.setattr(SystemRuntime, "_tip_layout_is_unknown", every_rack_is_unknown)

        joined = await submit(world, groups=[sample_group(2)], batch_mode=BatchMode.JOIN_EXISTING)

        assert joined == first
        first_transfer.set()
        world.runtime.close_execution(first)
        ended = await wait_for_the_end(world, first, timeout=120)
        assert set(ended.values()) == {"COMPLETED"}, ended
    finally:
        first_transfer.set()
        await close_world(world)
