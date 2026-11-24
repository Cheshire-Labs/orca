"""The approach direction, clearances and gripper offset of the target's access config reach the arm's place, and an edit reaches the next one."""

import pytest
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint
from cheshire_drivers.teachpoints import AccessConfig

from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import build_world, close_world, new_lab
from tests.sdk_features.settings_world import at, handling, registries, run_once

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

SIDE = AccessConfig(name="side", access_type="horizontal", gripper_offset=12.0, vertical_clearance=15.0, horizontal_clearance=80.0)


async def test_the_place_carries_the_access_config_and_follows_an_edit() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, "pad_1", "pad_2"))
    try:
        await registries(world).access_configs.add(SIDE, confirm=True)
        await world.runtime.teachpoints.update(
            "arm", Teachpoint("pad_2", C(300, 200, 300, 0, 90, 180), orientation="right", access=SIDE), confirm=True,
        )
        await run_once(world)
        [first] = at(world, "place_at_coords", "pad_2")
        assert (first.args["access_type"], first.args["horizontal_clearance"], first.args["vertical_clearance"]) == (
            "horizontal", 80.0, 15.0,
        )
        assert handling(first).access_type == "horizontal"

        await registries(world).access_configs.update(SIDE.model_copy(update={"horizontal_clearance": 60.0}), confirm=True)
        await run_once(world)
        _, second = at(world, "place_at_coords", "pad_2")
        assert second.args["horizontal_clearance"] == 60.0
    finally:
        await close_world(world)
