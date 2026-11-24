"""`PlateHotel`: a plate sent to a shelf is recorded at `<hotel>/shelf_<n>`, and two plates never share a shelf."""

import pytest

from orca.spawn import DISPENSE, LEAVE_IN_PLACE
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import (
    SHELVES,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    positions,
    submit,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


async def test_two_plates_are_recorded_on_two_shelves_and_the_arm_placed_each_there() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, ("stacker", DISPENSE), (list(SHELVES[:2]), LEAVE_IN_PLACE)))
    try:
        groups = [LabwareGroup(id=f"g{i}", members=(LabwareGroupMember("plate_journey"),)) for i in (1, 2)]
        execution_id = await submit(world, groups=groups)
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended

        shelves = sorted([(await plate_record(world, i)).location or "" for i in labware_of(world, execution_id, "plate")])
        assert shelves == ["hotel/shelf_1", "hotel/shelf_2"]
        assert sorted(p for p in positions(world, "arm", "place_at_coords") if p.startswith("hotel/")) == shelves
    finally:
        await close_world(world)
