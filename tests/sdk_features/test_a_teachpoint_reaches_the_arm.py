"""A move to a taught position sends the taught coordinates to the arm, and a changed teachpoint changes the next move."""

import pytest
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint

from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import build_world, close_world, new_lab
from tests.sdk_features.settings_world import at, coordinates, run_once

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_pick_carries_the_taught_coordinates_and_follows_an_edit() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, "pad_1", "pad_2"))
    try:
        await run_once(world)
        [first] = at(world, "pick_at_coords", "pad_1")
        assert coordinates(first)["x"] == 200.0

        await world.runtime.teachpoints.update(
            "arm", Teachpoint("pad_1", C(250, 210, 300, 0, 90, 180), orientation="right"), confirm=True,
        )
        await run_once(world)
        _, second = at(world, "pick_at_coords", "pad_1")
        assert (coordinates(second)["x"], coordinates(second)["y"]) == (250.0, 210.0)
    finally:
        await close_world(world)
