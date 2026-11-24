"""`GripperPad`: a carried plate is recorded in the gripper and nowhere else; after the place it is at the target and the gripper is empty; a second plate cannot go into a full gripper."""

import asyncio

import pytest

from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import (
    GRIPPER,
    Hold,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    submit,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_the_plate_is_in_the_gripper_during_the_move_and_at_the_target_after() -> None:
    lab = new_lab()
    held = Hold()
    lab.log("arm").holds["place_at_coords"] = held
    world = await build_world(lab, shake_once(lab, "pad_1", "pad_2"))
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(held.started.wait(), timeout=60)
        [plate_id] = labware_of(world, execution_id, "plate")
        assert (await plate_record(world, plate_id)).location == GRIPPER
        others_in_gripper = [s for s in await world.runtime.labware.list_all() if s.current_location == GRIPPER]
        assert [s.id for s in others_in_gripper] == [plate_id]

        with pytest.raises(Exception, match="(?i)occupied|full|already|holds"):
            await world.runtime.labware.register("plate", location=GRIPPER, barcode="SECOND", confirm=True)

        del lab.log("arm").holds["place_at_coords"]
        held.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        record = await plate_record(world, plate_id)
        assert record.location == "pad_2"
        assert [s for s in await world.runtime.labware.list_all() if s.current_location == GRIPPER] == []
    finally:
        await close_world(world)
