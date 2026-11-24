"""`orca.branch(event, {...})`: only the methods for the emitted value run, and an unlisted value runs `"else"`."""

import asyncio

import pytest

import orca.orca as orca
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from tests.sdk_features.event_world import event_workflow
from tests.sdk_features.kit import build_world, close_world, new_lab, run_to_end, speeds

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _branch(choices: dict[str, MethodTemplate]) -> IMethodTemplate:
    return orca.branch("go", {"a": [choices["a"]], "b": [choices["b"]], "else": [choices["else"]]})


@pytest.mark.parametrize(("value", "branch_speed"), [("b", 300.0), ("a", 200.0), ("unlisted", 400.0)])
async def test_only_the_chosen_branch_sends_commands(value: str, branch_speed: float) -> None:
    lab = new_lab()
    release = asyncio.Event()
    release.set()
    world = await build_world(lab, event_workflow(lab, release, value, _branch, name="branches"))
    try:
        _, ended = await run_to_end(world)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [100.0, branch_speed, 500.0]
    finally:
        await close_world(world)
