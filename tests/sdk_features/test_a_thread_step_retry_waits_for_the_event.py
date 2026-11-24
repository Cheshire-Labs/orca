"""After an `orca.on` timeout pauses the thread, RETRY waits for the event again."""

import asyncio

import pytest

import orca.orca as orca
from orca.workflow_models.method_template import IMethodTemplate, MethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from tests.sdk_features.event_world import event_workflow
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    recover,
    speeds,
    submit,
    wait_for_a_pause,
    wait_for_the_end,
)
from tests.test_helpers import let_the_loop_run

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


def _wait_with_timeout_then_a(choices: dict[str, MethodTemplate]) -> IMethodTemplate:
    return orca.on("go", timeout=0.5)


async def test_a_timeout_pauses_and_retry_waits_again() -> None:
    lab = new_lab()
    release = asyncio.Event()
    world = await build_world(lab, event_workflow(lab, release, "x", _wait_with_timeout_then_a, name="times_out"))
    try:
        execution_id = await submit(world)
        paused = await wait_for_a_pause(world, execution_id, template="waiter")
        assert paused.pause_reason == "error"

        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        await let_the_loop_run()
        assert speeds(world, "shaker_1") == [100.0], "RETRY waits for the event again"
        release.set()
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [100.0, 500.0]
    finally:
        await close_world(world)
