"""Availability semantics of a bridge-backed single-slot site.

A single-slot device holds ONE plate: 'loaded' means clamped in place on
the same physical position (owner ruling 2026-07-18), so the site reads
occupied from place until departure. Waiters on
``wait_until_available`` must block through the whole staged+loaded
span and wake on the pick - never on the stage->loaded transition (the
old loaded-reads-empty wake stacked plates).
"""

import asyncio

import pytest

from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location

from tests.mock import EXTERNAL_MOVER, UniversalMockDevice
from tests.test_helpers import create_test_labware_instance


@pytest.mark.asyncio
async def test_waiter_blocks_through_loaded_and_wakes_on_pick() -> None:
    device = UniversalMockDevice("lh")
    bridge = LabwareStagingBridge("lh", device)
    parent = Location("lh", bridge)

    plate = await create_test_labware_instance("plate1")
    await parent.notify_placed(plate, EXTERNAL_MOVER)
    assert plate in bridge.loaded_labware, "notify_placed loads internally"
    assert parent.labware is plate, "a loaded plate still occupies the site"

    waiter_task = asyncio.create_task(parent.wait_until_available(timeout=3.0))
    await asyncio.sleep(0)
    assert not waiter_task.done(), "waiter must block while the plate is loaded"

    await parent.prepare_for_pick(plate, EXTERNAL_MOVER)
    await parent.notify_picked(plate, EXTERNAL_MOVER)
    await asyncio.wait_for(waiter_task, timeout=2.0)
    assert parent.labware is None


@pytest.mark.asyncio
async def test_place_pick_churn_does_not_starve_waiter() -> None:
    """Consecutive occupants with a waiter in between: the waiter wakes on
    the departure that actually frees the site, not on any transition."""
    device = UniversalMockDevice("lh")
    bridge = LabwareStagingBridge("lh", device)
    parent = Location("lh", bridge)

    plate_a = await create_test_labware_instance("plate_a")
    plate_b = await create_test_labware_instance("plate_b")

    await parent.notify_placed(plate_a, EXTERNAL_MOVER)
    waiter_task = asyncio.create_task(parent.wait_until_available(timeout=3.0))
    await asyncio.sleep(0)
    assert not waiter_task.done()

    await parent.prepare_for_pick(plate_a, EXTERNAL_MOVER)
    await parent.notify_picked(plate_a, EXTERNAL_MOVER)
    await asyncio.wait_for(waiter_task, timeout=2.0)

    await parent.notify_placed(plate_b, EXTERNAL_MOVER)
    assert parent.labware is plate_b
