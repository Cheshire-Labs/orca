"""A move's speed comes from the arm's move parameters, with a carry patch on the plate applied on top."""

import asyncio

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch

from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import (
    Hold,
    build_world,
    close_world,
    labware_of,
    new_lab,
    submit,
    wait_for_the_end,
)
from tests.sdk_features.settings_world import at, handling, registries

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_stored_speed_reaches_the_pick_and_a_carry_patch_overrides_only_its_field() -> None:
    lab = new_lab()
    held = Hold()
    lab.log("shaker_1").holds["shake"] = held
    world = await build_world(lab, shake_once(lab, "pad_1", "pad_2"))
    try:
        await registries(world).move_defaults.apply("arm", MoveParameterPatch(speed=40.0), confirm=True)
        execution_id = await submit(world)
        await asyncio.wait_for(held.started.wait(), timeout=60)
        [plate_id] = labware_of(world, execution_id, "plate")
        await world.runtime.labware.set_carry_override(plate_id, MoveParameterPatch(speed=30.0), confirm=True)
        held.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended

        [before] = at(world, "pick_at_coords", "pad_1")
        [after] = at(world, "pick_at_coords", "shaker_1")
        assert handling(before).speed == 40.0
        assert handling(after).speed == 30.0
        assert handling(after).jaw_opening == handling(before).jaw_opening, "the patch names only the speed"
    finally:
        await close_world(world)
