"""A value changed during a run is read by every `ctx.param` after the change; reads before it saw the old value."""

import asyncio

import pytest

from orca.variables import VariableDefinition
from orca.variables.errors import VariableValidationError
from tests.sdk_features.kit import (
    FeatureWorld,
    build_world,
    close_world,
    new_lab,
    speeds,
    submit_one,
    wait_for_the_end,
)
from tests.sdk_features.variable_world import Reads, variable_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

DEFINITIONS = {"speed": VariableDefinition(type="int", default=400, min=100, max=1000)}


async def _held_between_actions(submitted: dict[str, int] | None = None) -> tuple[FeatureWorld, Reads, str, str]:
    lab = new_lab()
    reads = Reads("speed")
    reads.before_first_read.set()
    world = await build_world(lab, variable_workflow(lab, reads, DEFINITIONS))
    submission = await submit_one(world, variables=submitted)
    await asyncio.wait_for(reads.first_action_done.wait(), timeout=30)
    return world, reads, submission.execution_id, submission.id


async def test_a_change_on_the_submission_is_read_by_the_next_action() -> None:
    world, reads, execution_id, submission_id = await _held_between_actions({"speed": 500})
    try:
        world.runtime.variables.set_submission("speed", 700, execution_id, submission_id, confirm=True)
        reads.between_actions.set()
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert (reads.seen["first"], reads.seen["second"]) == ([500], [700])
        assert speeds(world, "shaker_1") == [500.0, 700.0]
    finally:
        await close_world(world)


async def test_a_change_on_the_execution_is_read_by_the_next_action() -> None:
    world, reads, execution_id, _ = await _held_between_actions()
    try:
        world.runtime.variables.set("speed", 800, execution_id, confirm=True)
        reads.between_actions.set()
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert (reads.seen["first"], reads.seen["second"]) == ([400], [800])
        assert speeds(world, "shaker_1") == [400.0, 800.0]
    finally:
        await close_world(world)


async def test_an_invalid_change_is_refused_and_the_run_keeps_the_old_value() -> None:
    world, reads, execution_id, submission_id = await _held_between_actions({"speed": 500})
    try:
        with pytest.raises(VariableValidationError):
            world.runtime.variables.set_submission("speed", 5000, execution_id, submission_id, confirm=True)
        with pytest.raises(VariableValidationError):
            world.runtime.variables.set("speed", 50, execution_id, confirm=True)
        reads.between_actions.set()
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert reads.seen["second"] == [500]
        assert speeds(world, "shaker_1") == [500.0, 500.0]
    finally:
        await close_world(world)
