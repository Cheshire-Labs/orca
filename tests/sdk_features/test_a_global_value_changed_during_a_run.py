"""A change to a global value is seen by a running workflow that reads `global.<name>` and has no submission or execution value."""

import asyncio

import pytest

from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    speeds,
    submit_one,
    wait_for_the_end,
)
from tests.sdk_features.variable_world import Reads, variable_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_next_action_reads_the_changed_global_value() -> None:
    lab = new_lab()
    reads = Reads("global.speed")
    reads.before_first_read.set()
    world = await build_world(lab, variable_workflow(lab, reads, {}))
    try:
        await world.runtime.variables.set_global("speed", 420, confirm=True)
        submission = await submit_one(world)
        await asyncio.wait_for(reads.first_action_done.wait(), timeout=30)

        await world.runtime.variables.set_global("speed", 520, confirm=True)
        reads.between_actions.set()
        ended = await wait_for_the_end(world, submission.execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert (reads.seen["first"], reads.seen["second"]) == ([420], [520])
        assert speeds(world, "shaker_1") == [420.0, 520.0]
    finally:
        await close_world(world)
