"""`wf.variable` gives every context its default when nothing is submitted, and a value outside the definition is refused at submit."""

import pytest

from orca.variables import OptionValue, VariableDefinition
from orca.variables.errors import SubmissionInputsRefusedError
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    speeds,
    submit,
    wait_for_the_end,
)
from tests.sdk_features.variable_world import Reads, variable_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

DEFINITIONS = {
    "speed": VariableDefinition(type="int", default=400, min=100, max=1000),
    "mode": VariableDefinition(type="str", default="gentle", allowed_values=["gentle", "hard"]),
}


async def test_every_context_reads_the_default_when_nothing_is_submitted() -> None:
    lab = new_lab()
    reads = Reads("speed")
    reads.before_first_read.set()
    reads.between_actions.set()
    world = await build_world(lab, variable_workflow(lab, reads, DEFINITIONS))
    try:
        execution_id = await submit(world)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert reads.seen == {"thread": [400], "method": [400], "first": [400], "second": [400]}
        assert speeds(world, "shaker_1") == [400.0, 400.0]
    finally:
        await close_world(world)


@pytest.mark.parametrize("variables", [
    {"speed": "fast"},
    {"speed": 50},
    {"speed": 5000},
    {"mode": "violent"},
], ids=["wrong type", "below min", "above max", "not allowed"])
async def test_a_value_outside_the_definition_is_refused_at_submit(variables: dict[str, OptionValue]) -> None:
    lab = new_lab()
    reads = Reads("speed")
    world = await build_world(lab, variable_workflow(lab, reads, DEFINITIONS))
    try:
        with pytest.raises(SubmissionInputsRefusedError):
            await submit(world, variables=variables)

        assert reads.seen["thread"] == []
        assert speeds(world, "shaker_1") == []
    finally:
        await close_world(world)
