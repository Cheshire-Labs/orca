"""After a `ctx.wait_for` timeout the thread pauses, and RETRY waits again without re-sending the commands before it."""

import pytest

from orca.workflow_models.status_enums import RecoveryDecision
from tests.sdk_features.kit import (
    build_world,
    close_world,
    commands,
    new_lab,
    recover,
    submit,
    wait_for_a_pause,
    wait_for_the_end,
)
from tests.sdk_features.test_wait_for_holds_the_thread_until_the_event import (
    _Signals,
    _waiter_and_emitter,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_a_timeout_pauses_and_retry_waits_again_without_resending_earlier_commands() -> None:
    lab = new_lab()
    signals = _Signals()
    world = await build_world(lab, _waiter_and_emitter(lab, signals, emit_first=False, timeout=5))
    try:
        execution_id = await submit(world)
        paused = await wait_for_a_pause(world, execution_id, template="waiter")
        assert paused.pause_reason == "error"
        assert "ran out of time" in (paused.pause_message or "")

        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        assert signals.emitted_at is None, "the retried wait must catch an event sent after the RETRY"
        signals.release_the_emit.set()
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert [c.args["speed"] for c in commands(world, "shaker_1")] == [100.0, 200.0], (
            "RETRY repeats only the wait, not the shake before it"
        )
    finally:
        await close_world(world)
