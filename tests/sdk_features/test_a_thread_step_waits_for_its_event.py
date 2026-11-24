"""`orca.on(event)`: the thread waits for the event, then continues.

Timeout then RETRY: `test_a_thread_step_retry_waits_for_the_event.py`.
"""

import asyncio

import pytest

import orca.orca as orca
from tests.sdk_features.event_world import event_workflow
from tests.sdk_features.kit import (
    build_world,
    close_world,
    commands,
    new_lab,
    speeds,
    submit,
    wait_for_the_end,
)
from tests.test_helpers import let_the_loop_run, wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_thread_continues_only_after_the_event() -> None:
    lab = new_lab()
    release = asyncio.Event()
    emitted_at: list[float] = []
    world = await build_world(
        lab, event_workflow(lab, release, "x", lambda _: orca.on("go"), name="waits", emitted_at=emitted_at),
    )
    try:
        execution_id = await submit(world)
        await wait_until(lambda: speeds(world, "shaker_1") == [100.0], timeout=60)
        await let_the_loop_run()
        assert speeds(world, "shaker_1") == [100.0]

        release.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [100.0, 500.0]
        assert commands(world, "shaker_1")[1].started >= emitted_at[0], "the step after the wait starts after the emit"
    finally:
        await close_world(world)
