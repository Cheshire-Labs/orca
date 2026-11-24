"""No mover reaches into a device's site between the record freeing it and the device being told the plate left.

The position effect frees the site on record first. Another mover is granted the site at that
moment. A pick holds the site's device lock across both records, so that mover's reach-in waits until the device
knows; without it the device still holds the plate and refuses the mover's place.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from orca.resource_models.command_effects import Sender, arm_pick, pick_with_the_arm
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.location import Location
from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.transporter_base import TransporterBase
from tests.mock import EXTERNAL_MOVER
from tests.test_helpers import command_pipeline_over, create_test_labware_instance

pytestmark = pytest.mark.timeout(30)


async def _send() -> None:
    return None


async def _record(_result: None) -> None:
    return None


async def test_a_mover_granted_the_freed_site_reaches_in_only_after_the_device_is_told() -> None:
    order: list[str] = []
    device = Mock(spec=Device)
    device.lock = TrackedLock("lh device lock")
    device._do_notify_placed = AsyncMock()
    device._do_notify_picked = AsyncMock(side_effect=lambda *_a, **_k: order.append("device told the plate left"))
    site = Location("lh/carrier-7-2", DeviceDeckSite("lh/carrier-7-2", device))
    plate = await create_test_labware_instance("plate_1")
    await site.resource.notify_placed(plate, EXTERNAL_MOVER)
    reaches: list[asyncio.Task[None]] = []

    async def gripper_reaches_in() -> None:
        async with device.lock.held_for("gripper place"):
            order.append("gripper reaches in")

    async def record_frees_the_site(_labware: LabwareInstance, _jaws: Location) -> None:
        order.append("record frees the site")
        reaches.append(asyncio.create_task(gripper_reaches_in()))
        for _ in range(5):
            await asyncio.sleep(0)

    placer = Mock(spec=LabwarePlacer)
    placer.picked_up = AsyncMock(side_effect=record_frees_the_site)
    arm = Mock(spec=TransporterBase)
    arm.name = "arm"
    arm.gripper_location = Mock(spec=Location)
    arm.pick_moves_the_plate = False

    await command_pipeline_over(placer).run(
        "arm", "pick_at_coords", _send, _record, statement=arm_pick(arm, site, plate),
        checks=(), params=None, execution_id=None, events=None, carries_out=None,
    )
    await asyncio.gather(*reaches)

    assert order == ["record frees the site", "device told the plate left", "gripper reaches in"]


async def test_a_pick_a_person_marks_complete_holds_the_device_across_its_records() -> None:
    order: list[str] = []
    device = Mock(spec=Device)
    device.lock = TrackedLock("lh device lock")
    device._do_notify_placed = AsyncMock()
    device._do_notify_picked = AsyncMock(side_effect=lambda *_a, **_k: order.append("device told the plate left"))
    site = Location("lh/carrier-7-2", DeviceDeckSite("lh/carrier-7-2", device))
    plate = await create_test_labware_instance("plate_1")
    await site.resource.notify_placed(plate, EXTERNAL_MOVER)
    reaches: list[asyncio.Task[None]] = []

    async def gripper_reaches_in() -> None:
        async with device.lock.held_for("gripper place"):
            order.append("gripper reaches in")

    async def record_frees_the_site(_labware: LabwareInstance, _jaws: Location) -> None:
        order.append("record frees the site")
        reaches.append(asyncio.create_task(gripper_reaches_in()))
        for _ in range(5):
            await asyncio.sleep(0)

    placer = Mock(spec=LabwarePlacer)
    placer.picked_up = AsyncMock(side_effect=record_frees_the_site)
    arm = Mock(spec=TransporterBase)
    arm.name = "arm"
    arm.gripper_location = Mock(spec=Location)

    await command_pipeline_over(placer).apply_completed(arm_pick(arm, site, plate))
    await asyncio.gather(*reaches)

    assert order == ["record frees the site", "device told the plate left", "gripper reaches in"]


async def test_a_task_holding_a_lock_takes_it_again_and_another_task_still_waits() -> None:
    lock = TrackedLock("lh device lock")
    order: list[str] = []

    async def other() -> None:
        async with lock.held_for("other"):
            order.append("other")

    async with lock.held_for("outer"):
        waiting = asyncio.create_task(other())
        async with lock.held_for("inner"):
            await asyncio.sleep(0)
            order.append("inner")
        await asyncio.sleep(0)
        order.append("outer still held")
    await waiting

    assert order == ["inner", "outer still held", "other"]


async def test_a_command_queued_on_the_device_during_the_pick_runs_only_after_its_records() -> None:
    order: list[str] = []
    device = Mock(spec=Device)
    device.lock = TrackedLock("lh device lock")
    device._do_notify_placed = AsyncMock()
    device._do_notify_picked = AsyncMock(side_effect=lambda *_a, **_k: order.append("device told the plate left"))
    site = Location("lh/carrier-7-2", DeviceDeckSite("lh/carrier-7-2", device))
    plate = await create_test_labware_instance("plate_1")
    await site.resource.notify_placed(plate, EXTERNAL_MOVER)
    queued: list[asyncio.Task[None]] = []

    async def a_long_method() -> None:
        async with device.lock.held_for("venus method"):
            order.append("queued command runs")

    @asynccontextmanager
    async def the_lease(_site: Location, _purpose: str) -> AsyncIterator[None]:
        async with device.lock.held_for("pick lease"):
            yield

    async def pick(_site: Location) -> None:
        async with the_lease(site, "pick"):
            queued.append(asyncio.create_task(a_long_method()))
            await asyncio.sleep(0)
            order.append("arm picked")

    async def record_frees_the_site(_labware: LabwareInstance, _jaws: Location) -> None:
        order.append("record frees the site")

    placer = Mock(spec=LabwarePlacer)
    placer.picked_up = AsyncMock(side_effect=record_frees_the_site)
    arm = Mock(spec=TransporterBase)
    arm.name = "arm"
    arm.gripper_location = Mock(spec=Location)
    arm.pick_moves_the_plate = False
    arm.pick = AsyncMock(side_effect=pick)
    arm.holding_the_site_through_its_records = the_lease

    await pick_with_the_arm(
        command_pipeline_over(placer), arm, site, plate,
        Sender(record=_record, checks=(), execution_id=None, events=None),
    )
    await asyncio.gather(*queued)

    assert order == ["arm picked", "record frees the site", "device told the plate left", "queued command runs"]


async def test_an_arm_already_holding_the_device_reaches_in_without_a_second_lease() -> None:
    device = Mock(spec=Device)
    device.lock = TrackedLock("lh device lock")
    site = Location("lh/carrier-7-2", DeviceDeckSite("lh/carrier-7-2", device))
    arm = Mock(spec=TransporterBase)
    arm._name = "arm"

    async with device.lock.held_for("pick and its records"):
        async with TransporterBase.reach_into(arm, site, "pick"):
            pass

    device.reach_in_lease.assert_not_called()
