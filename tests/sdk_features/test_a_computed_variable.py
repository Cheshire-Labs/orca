"""A deployment profile's computed variable equals its expression worked out from the other values, and follows a change to an input."""

import asyncio

import pytest

from orca.variables import VariableDefinition
from orca.variables.deployment_profile import DeploymentProfile
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


async def test_a_computed_read_follows_its_input() -> None:
    lab = new_lab()
    reads = Reads("speed")
    reads.before_first_read.set()
    definitions = {"base": VariableDefinition(type="int", default=200)}
    world = await build_world(lab, variable_workflow(lab, reads, definitions))
    try:
        await world.runtime.profile_store.add(DeploymentProfile(name="doubled", computed={"speed": "base * 2 + 1"}))
        submission = await submit_one(world, deployment_profile="doubled")
        await asyncio.wait_for(reads.first_action_done.wait(), timeout=30)

        world.runtime.variables.set("base", 300, submission.execution_id, confirm=True)
        reads.between_actions.set()
        ended = await wait_for_the_end(world, submission.execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert (reads.seen["first"], reads.seen["second"]) == ([401], [601])
        assert speeds(world, "shaker_1") == [401.0, 601.0]
    finally:
        await close_world(world)
