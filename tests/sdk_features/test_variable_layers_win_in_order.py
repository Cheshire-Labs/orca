"""A workflow variable resolves submission, execution, computed, default, and never a global value.

A `global.` variable resolves submission, execution, global value, computed, default. One test per layer.
"""

from collections.abc import Awaitable, Callable

import pytest

from orca.variables import OptionValue, VariableDefinition
from orca.variables.deployment_profile import DeploymentProfile
from tests.sdk_features.kit import (
    FeatureWorld,
    build_world,
    close_world,
    new_lab,
    speeds,
    statuses,
    submit_one,
    wait_for_the_end,
)
from tests.sdk_features.variable_world import Reads, variable_workflow
from tests.test_helpers import wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

Layer = Callable[[FeatureWorld, str], Awaitable[None]]


async def _no_layer(world: FeatureWorld, execution_id: str) -> None:
    return None


async def _read_with(
    variable: str,
    *,
    submitted: dict[str, OptionValue] | None = None,
    profile: DeploymentProfile | None = None,
    global_value: int | None = None,
    global_default: int | None = None,
    while_held: Layer = _no_layer,
) -> list[float]:
    """Set the layers, let the first action read, and return the speeds both actions shook at."""
    lab = new_lab()
    reads = Reads(variable)
    reads.between_actions.set()
    definitions = {} if variable.startswith("global.") else {variable: VariableDefinition(type="int", default=400)}
    world = await build_world(lab, variable_workflow(lab, reads, definitions))
    try:
        if global_default is not None:
            world.runtime.system.variable_store.register_global_definitions(
                {variable.removeprefix("global."): VariableDefinition(type="int", default=global_default)},
            )
        if global_value is not None:
            await world.runtime.variables.set_global(variable.removeprefix("global."), global_value, confirm=True)
        if profile is not None:
            await world.runtime.profile_store.add(profile)
        submission = await submit_one(
            world, variables=submitted, deployment_profile=None if profile is None else profile.name,
        )
        await while_held(world, submission.execution_id)
        reads.before_first_read.set()
        ended = await wait_for_the_end(world, submission.execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert reads.seen["first"] == reads.seen["second"]
        return speeds(world, "shaker_1")
    finally:
        await close_world(world)


def _execution_value(variable: str, value: int) -> Layer:
    async def set_it(world: FeatureWorld, execution_id: str) -> None:
        await wait_until(lambda: bool(statuses(world, execution_id)), timeout=30)
        world.runtime.variables.set(variable, value, execution_id, confirm=True)
    return set_it


def _computed(name: str, expression: str) -> DeploymentProfile:
    return DeploymentProfile(name="computed", computed={name: expression})


async def test_a_workflow_variable_reads_its_default() -> None:
    assert await _read_with("speed") == [400.0, 400.0]


async def test_a_workflow_variable_reads_computed_over_its_default() -> None:
    assert await _read_with("speed", profile=_computed("speed", "300 + 1")) == [301.0, 301.0]


async def test_a_workflow_variable_reads_its_execution_value_over_computed() -> None:
    speeds = await _read_with("speed", profile=_computed("speed", "301"), while_held=_execution_value("speed", 500))
    assert speeds == [500.0, 500.0]


async def test_a_workflow_variable_reads_its_submission_value_over_the_execution_value() -> None:
    speeds = await _read_with("speed", submitted={"speed": 600}, while_held=_execution_value("speed", 500))
    assert speeds == [600.0, 600.0]


async def test_a_workflow_variable_never_reads_a_global_value() -> None:
    assert await _read_with("speed", global_value=999) == [400.0, 400.0]


async def test_a_global_variable_reads_its_default() -> None:
    assert await _read_with("global.speed", global_default=410) == [410.0, 410.0]


async def test_a_global_variable_reads_computed_over_its_default() -> None:
    assert await _read_with("global.speed", global_default=410, profile=_computed("speed", "311")) == [311.0, 311.0]


async def test_a_global_variable_reads_the_global_value_over_computed() -> None:
    speeds = await _read_with("global.speed", global_value=420, profile=_computed("speed", "311"))
    assert speeds == [420.0, 420.0]


async def test_a_global_variable_reads_its_execution_value_over_the_global_value() -> None:
    speeds = await _read_with("global.speed", global_value=420, while_held=_execution_value("global.speed", 430))
    assert speeds == [430.0, 430.0]


async def test_a_global_variable_reads_its_submission_value_over_the_execution_value() -> None:
    speeds = await _read_with(
        "global.speed", global_value=420, submitted={"global.speed": 440},
        while_held=_execution_value("global.speed", 430),
    )
    assert speeds == [440.0, 440.0]
