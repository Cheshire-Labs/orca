"""After an `orca.branch` timeout the thread pauses where `orca.on` does, and RETRY waits for a new event."""

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


def _branch_with_timeout(choices: dict[str, MethodTemplate]) -> IMethodTemplate:
    return orca.branch("go", {"a": [choices["a"]], "else": [choices["else"]]}, timeout=1.0)


async def test_a_branch_timeout_pauses_for_retry_and_retry_runs_the_branch_of_a_later_event() -> None:
    lab = new_lab()
    release = asyncio.Event()
    world = await build_world(lab, event_workflow(lab, release, "a", _branch_with_timeout, name="branch_times_out"))
    try:
        execution_id = await submit(world)
        paused = await wait_for_a_pause(world, execution_id, template="waiter")
        assert paused.pause_reason == "error"
        assert paused.pause_site == "WAIT_EVENT_TIMEOUT"
        assert paused.honoured_decisions == ("RETRY", "ABORT_THREAD")

        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        await let_the_loop_run()
        assert speeds(world, "shaker_1") == [100.0], "RETRY waits for the event again"
        release.set()
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [100.0, 200.0, 500.0], (
            "RETRY repeats only the wait, then runs the branch the event picks"
        )
    finally:
        await close_world(world)


async def test_abort_thread_after_a_branch_timeout_ends_the_thread_with_one_decision() -> None:
    lab = new_lab()
    release = asyncio.Event()
    world = await build_world(lab, event_workflow(lab, release, "a", _branch_with_timeout, name="branch_aborted"))
    try:
        execution_id = await submit(world)
        paused = await wait_for_a_pause(world, execution_id, template="waiter")

        await recover(world, execution_id, paused.id, RecoveryDecision.ABORT_THREAD)
        release.set()
        ended = await wait_for_the_end(world, execution_id, timeout=30.0)

        assert sorted(ended.values()) == ["ABORTED", "COMPLETED"], ended
        assert speeds(world, "shaker_1") == [100.0], "an aborted branch runs none of its methods"
    finally:
        await close_world(world)
