"""`ResourcePool`: each step runs on one pool device and the record names it; a busy device is passed over; all busy means wait; two plates run at once.

An offline pool device is never chosen: that is pinned by `tests/runtime/test_an_offline_live_device_refuses_a_run_at_its_start.py`,
because a device is marked offline only when a LIVE run's start cannot bring it up.
"""

import asyncio

import pytest

from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from tests.sdk_features.kit import (
    Hold,
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    speeds,
    submit,
    wait_for_the_end,
)
from tests.sdk_features.pool_world import PINNED, POOLED, pool_workflow
from tests.test_helpers import let_the_loop_run, wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

def _shaker_in(history: list[str]) -> list[str]:
    return [name for name in ("shaker_1", "shaker_2") if any(h.split("/")[0] == name for h in history)]


async def test_the_record_names_the_pool_device_the_step_ran_on() -> None:
    lab = new_lab()
    world = await build_world(lab, pool_workflow(lab))
    try:
        execution_id = await submit(world)
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended

        [plate_id] = labware_of(world, execution_id, "pooled")
        [used] = _shaker_in((await plate_record(world, plate_id)).history)
        other = "shaker_2" if used == "shaker_1" else "shaker_1"
        assert speeds(world, used) == [POOLED]
        assert speeds(world, other) == []
    finally:
        await close_world(world)


async def test_two_plates_run_at_the_same_time_on_different_devices() -> None:
    lab = new_lab()
    holds = {name: Hold() for name in ("shaker_1", "shaker_2")}
    for name, hold in holds.items():
        lab.log(name).holds["shake"] = hold
    world = await build_world(lab, pool_workflow(lab))
    try:
        groups = [LabwareGroup(id=f"g{i}", members=(LabwareGroupMember("pooled_journey"),)) for i in (1, 2)]
        execution_id = await submit(world, groups=groups)
        await asyncio.wait_for(asyncio.gather(*(h.started.wait() for h in holds.values())), timeout=60)

        for hold in holds.values():
            hold.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        used = [_shaker_in((await plate_record(world, i)).history) for i in labware_of(world, execution_id, "pooled")]
        assert sorted(u for [u] in used) == ["shaker_1", "shaker_2"]
    finally:
        await close_world(world)


async def test_a_busy_pool_device_is_passed_over_for_a_free_one() -> None:
    lab = new_lab()
    busy = Hold()
    lab.log("shaker_1").holds["shake"] = busy
    world = await build_world(lab, pool_workflow(lab, pin_1=True))
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(busy.started.wait(), timeout=60)
        await wait_until(lambda: speeds(world, "shaker_2") == [POOLED], timeout=60)
        assert speeds(world, "shaker_1") == [PINNED]

        busy.finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [PINNED]
    finally:
        await close_world(world)


async def test_when_every_pool_device_is_busy_the_step_waits_for_one_to_free() -> None:
    lab = new_lab()
    holds = {name: Hold() for name in ("shaker_1", "shaker_2")}
    for name, hold in holds.items():
        lab.log(name).holds["shake"] = hold
    world = await build_world(lab, pool_workflow(lab, pin_1=True, pin_2=True))
    try:
        execution_id = await submit(world)
        await asyncio.wait_for(asyncio.gather(*(h.started.wait() for h in holds.values())), timeout=60)
        await let_the_loop_run()
        assert speeds(world, "shaker_1") + speeds(world, "shaker_2") == [PINNED, PINNED]

        del lab.log("shaker_1").holds["shake"]
        holds["shaker_1"].finish.set()
        await wait_until(lambda: speeds(world, "shaker_1") == [PINNED, POOLED], timeout=60)
        assert speeds(world, "shaker_2") == [PINNED]

        holds["shaker_2"].finish.set()
        ended = await wait_for_the_end(world, execution_id)
        assert set(ended.values()) == {"COMPLETED"}, ended
    finally:
        await close_world(world)
