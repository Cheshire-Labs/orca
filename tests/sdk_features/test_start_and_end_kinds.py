"""Start kinds `MANUAL_PLACE`, `DISPENSE`, `REUSE_EXISTING` and end kinds `LEAVE_IN_PLACE` and a list of spots, in a SIM run.

`MANUAL_REMOVE` in SIM: `test_a_manually_removed_plate_leaves_the_system.py`.
"""

import asyncio

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.spawn import (
    DISPENSE,
    LEAVE_IN_PLACE,
    MANUAL_PLACE,
)
from orca.state.placement import PlacementState
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from tests.sdk_features.device_world import shake_once, shake_then_take
from tests.sdk_features.kit import (
    SHELVES,
    Hold,
    build_world,
    close_world,
    commands,
    labware_of,
    new_lab,
    plate_record,
    speeds,
    submit,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

SIM = WorkflowRunMode.SIM


async def test_manual_place_in_sim_puts_the_plate_at_its_start_without_a_person() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, ("pad_1", MANUAL_PLACE), ("pad_2", LEAVE_IN_PLACE)))
    try:
        execution_id = await submit(world, mode=SIM)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        [plate_id] = labware_of(world, execution_id, "plate")
        record = await plate_record(world, plate_id)
        assert record.history[0] == "pad_1"
        assert speeds(world, "shaker_1") == [100.0]
    finally:
        await close_world(world)


async def test_dispense_calls_the_source_before_the_plate_is_on_the_record() -> None:
    lab = new_lab()
    held = Hold()
    lab.log("stacker").holds["dispense"] = held
    world = await build_world(lab, shake_once(lab, ("stacker", DISPENSE), ("pad_2", LEAVE_IN_PLACE)))
    try:
        execution_id = await submit(world, mode=SIM)
        await asyncio.wait_for(held.started.wait(), timeout=60)
        there = [s for s in await world.runtime.labware.list_all() if s.current_location == "stacker/slot"]
        assert [s.placement for s in there if s.placement is PlacementState.PRESENT] == [], (
            "no plate is recorded as present at the stacker while it is still dispensing"
        )

        held.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        [plate_id] = labware_of(world, execution_id, "plate")
        assert (await plate_record(world, plate_id)).history[0] == "stacker/slot"
        assert len(commands(world, "stacker", "dispense")) == 1
    finally:
        await close_world(world)


async def test_reuse_existing_binds_the_same_rack_on_the_second_run() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_then_take(lab))
    try:
        first = await submit(world, mode=SIM)
        assert set((await wait_for_the_end(world, first)).values()) == {"COMPLETED"}
        second = await submit(world, mode=SIM)
        assert set((await wait_for_the_end(world, second)).values()) == {"COMPLETED"}

        [rack] = labware_of(world, first, "tips")
        assert labware_of(world, second, "tips") == [rack]
        assert len([s for s in await world.runtime.labware.list_all() if s.template_name == "tips"]) == 1
        picks = [c.args["picks"] for c in commands(world, "lh", "pick_up_tips")]
        assert len(picks) == 2 and picks[0] != picks[1], "the second run takes the next tips from the same rack"
    finally:
        await close_world(world)


async def test_leave_in_place_keeps_the_plate_and_its_volumes_on_the_record() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, "pad_1", ("pad_2", LEAVE_IN_PLACE)))
    try:
        execution_id = await submit(world, mode=SIM)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        [plate_id] = labware_of(world, execution_id, "plate")
        record = await plate_record(world, plate_id)
        assert record.location == "pad_2"
        assert record.volumes["A1"] == 100.0 and len(record.volumes) == 96
    finally:
        await close_world(world)


async def test_a_list_of_end_spots_puts_concurrent_plates_on_different_spots() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, ("stacker", DISPENSE), (list(SHELVES), LEAVE_IN_PLACE)))
    try:
        groups = [LabwareGroup(id=f"g{i}", members=(LabwareGroupMember("plate_journey"),)) for i in (1, 2)]
        execution_id = await submit(world, groups=groups, mode=SIM)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        ends = [(await plate_record(world, i)).location for i in labware_of(world, execution_id, "plate")]
        assert len(ends) == 2 and len(set(ends)) == 2 and set(ends) <= set(SHELVES)
    finally:
        await close_world(world)
