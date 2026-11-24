"""`HumanTransfer`: each pick and place names its site, and in a simulated run the move completes without a person."""

import pytest

from orca.driver_management.drivers.human_transfer import HumanTransfer
from orca.resource_models.plate_pad import PlatePad
from tests.real_device_path import the_real_device_path
from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import (
    KitArm,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    positions,
    run_to_end,
    taught,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_a_person_carries_the_plate_to_a_bench_only_they_reach() -> None:
    lab = new_lab()
    person = KitArm("person")
    the_real_device_path().simulate_with("person", person)
    lab.drivers["person"] = person
    lab.more_transporters.append(taught(lab.stores, HumanTransfer("person"), ["pad_2", "bench"]))
    lab.more_locations["bench"] = PlatePad("bench")
    world = await build_world(lab, shake_once(lab, "pad_1", "bench"))
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        [plate_id] = labware_of(world, execution_id, "plate")
        assert (await plate_record(world, plate_id)).location == "bench"
        assert positions(world, "person", "pick_at_coords") == ["pad_2"]
        assert positions(world, "person", "place_at_coords") == ["bench"]
    finally:
        await close_world(world)
