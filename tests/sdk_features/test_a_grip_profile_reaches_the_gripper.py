"""A pick uses the carried labware type's grip profile; a type with no profile uses the arm's default."""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch

from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import PLATE_TYPE, build_world, close_world, new_lab
from tests.sdk_features.settings_world import at, handling, registries, run_once

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_pick_uses_the_profile_when_there_is_one_and_the_default_when_not() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, "pad_1", "pad_2"))
    try:
        await run_once(world)
        [unprofiled] = at(world, "pick_at_coords", "pad_1")
        default = (await registries(world).move_defaults.get("arm")).parameters.jaw_opening
        assert handling(unprofiled).jaw_opening == default != 21.0

        await registries(world).grip_profiles.apply(PLATE_TYPE, MoveParameterPatch(jaw_opening=21.0), confirm=True)
        await run_once(world)
        _, profiled = at(world, "pick_at_coords", "pad_1")
        assert handling(profiled).jaw_opening == 21.0
    finally:
        await close_world(world)
