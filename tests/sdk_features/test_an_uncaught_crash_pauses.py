"""A crash the thread's own error handling does not catch pauses the thread, offering ABORT_THREAD only."""

import pytest

from tests.sdk_features.crash_world import crash_when_starting
from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import build_world, close_world, new_lab, run_to_pause

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_a_crash_pauses_the_thread_for_an_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    crash_when_starting(monkeypatch, "plate")
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, "pad_1", "pad_2"))
    try:
        _, paused = await run_to_pause(world)

        assert "crash while starting plate" in (paused.last_error or "") + (paused.pause_message or "")
        assert paused.pause_site == "UNHANDLED_ERROR"
        assert paused.honoured_decisions == ("ABORT_THREAD",)
    finally:
        await close_world(world)
