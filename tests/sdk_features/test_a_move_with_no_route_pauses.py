"""A move whose first plan finds no route pauses the thread at MOVE_RESOLUTION, naming the place it cannot reach."""

import pytest

from orca.resource_models.plate_pad import PlatePad
from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    run_to_pause,
    speeds,
    statuses,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_an_end_no_transporter_reaches_pauses_the_thread() -> None:
    lab = new_lab()
    lab.more_locations["island"] = PlatePad("island")
    world = await build_world(lab, shake_once(lab, "pad_1", "island"))
    try:
        execution_id, paused = await run_to_pause(world)

        assert paused.pause_reason == "error"
        assert paused.pause_site == "MOVE_RESOLUTION"
        assert "island" in (paused.last_error or "") + (paused.pause_message or "")
        assert speeds(world, "shaker_1") == [100.0]
        assert list(statuses(world, execution_id).values()) == ["PAUSED"]
    finally:
        await close_world(world)
