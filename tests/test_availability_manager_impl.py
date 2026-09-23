"""Round 5 S1-B partial: ``LocationReservationManager`` now implements
the previously-stubbed ``IAvailabilityManager`` methods.

The full S1-B architectural restoration (remove the labware-occupancy
check from ``can_reserve`` so the reservation layer is purely
holder-exclusivity per ``IReservationManager``) is deferred -- the
``ThreadDeadlockDetector`` currently keys off ``rejected`` reservations
from occupancy collisions, so removing that signal here without
rewiring the detector to consume an ``IAvailabilityManager`` signal
produces a cross-tick deadlock-detection regression. The follow-up
session that takes this on rewires both layers in lockstep.

These tests cover the additive part that DID land: the previously-stub
``IAvailabilityManager.is_location_available`` / ``await_available``
methods now delegate to ``Location.labware`` / ``Location.wait_until_available``
on the same manager, so future callers can ask through the typed
interface instead of reaching into Location internals.
"""

import asyncio
from unittest.mock import Mock

import pytest

from orca.resource_models.labware import PlateInstance
from orca.resource_models.location import Location
from orca.runtime.sim_labware import SimPlate
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.reservation_manager import LocationReservationManager


def _plate(name: str) -> PlateInstance:
    return PlateInstance(SimPlate(name), template_name="plate_96", labware_type="plate_96")


def _make_location_reg(location: Location) -> Mock:
    reg = Mock()
    reg.get_location.return_value = location
    return reg


def test_availability_manager_reports_empty_location() -> None:
    """``IAvailabilityManager.is_location_available`` -> True when labware is None."""
    pad = Location("pad_1", PlatePad("pad_1"))
    manager = LocationReservationManager(_make_location_reg(pad))

    assert manager.is_location_available(pad) is True


def test_availability_manager_reports_occupied_location() -> None:
    """``IAvailabilityManager.is_location_available`` -> False when occupied."""
    plate_pad = PlatePad("pad_1")
    pad = Location("pad_1", plate_pad)
    plate_pad.initialize_labware(_plate("plate_1"))
    manager = LocationReservationManager(_make_location_reg(pad))

    assert manager.is_location_available(pad) is False


@pytest.mark.asyncio
async def test_await_available_unblocks_on_labware_picked() -> None:
    """``await_available`` delegates to ``Location.wait_until_available()``.

    Park on the wait, simulate a labware pick via ``notify_picked``,
    assert the await unblocks. This is the contract the move layer
    relies on via ``executing_labware_thread.py:1388-1389``.
    """
    plate_pad = PlatePad("pad_1")
    pad = Location("pad_1", plate_pad)
    labware = _plate("plate_1")
    plate_pad.initialize_labware(labware)
    manager = LocationReservationManager(_make_location_reg(pad))

    async def picker() -> None:
        await asyncio.sleep(0)  # let the awaiter park first
        await plate_pad.dispose_labware(labware)
        async with pad._availability_condition:
            pad._availability_condition.notify_all()

    await asyncio.gather(
        manager.await_available(pad),
        picker(),
    )
    assert pad.labware is None
