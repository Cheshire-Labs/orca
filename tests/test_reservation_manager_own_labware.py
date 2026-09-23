"""Round 5 S1-B full: ``can_reserve`` is thread-aware about labware identity.

The user's Round 5 session repro: an entry thread with its own labware
at its own start_location stalled forever -- ``can_reserve`` rejected
on labware presence at the target without recognizing that the labware
WAS the requesting thread's own. The retry loop spun forever because no
other thread would ever "release" something that was never separately
reserved.

The fix: ``can_reserve`` accepts ``requesting_labware_id`` and grants
when the location's current labware matches it. Cross-thread occupancy
still rejects so ``ThreadDeadlockDetector`` keeps seeing the cycle
signal that drives recovery.
"""

import pytest
from unittest.mock import Mock

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import LocationReservationManager


def _make_location_reg(location: Location) -> Mock:
    reg = Mock()
    reg.get_location.return_value = location
    return reg


def _make_labware(name: str) -> LabwareInstance:
    return LabwareInstance(name, "plate")


def test_can_reserve_grants_own_labware_at_target() -> None:
    """User's session repro: own labware at own start_location -> True.

    Pre-fix returned False because the layer could not tell own labware
    from cross-thread occupancy; the entry thread's first action
    against its own start_location stalled forever.
    """
    pad_resource = PlatePad("pad_1")
    my_plate = _make_labware("my_plate")
    pad_resource.initialize_labware(my_plate)
    pad = Location("pad_1", pad_resource)
    manager = LocationReservationManager(_make_location_reg(pad))

    assert manager.can_reserve(
        "pad_1", thread_id="thread_A", requesting_labware_id=my_plate.id,
    ) is True


def test_can_reserve_rejects_cross_thread_labware_at_target() -> None:
    """Cross-thread occupancy still rejects so deadlock detector sees the cycle.

    Pre-fix and post-fix both reject this case -- the deadlock detector
    needs the rejection signal to find cross-thread blocking cycles.
    The labware id MUST not match the requester's labware.
    """
    pad_resource = PlatePad("pad_1")
    other_thread_plate = _make_labware("other_plate")
    pad_resource.initialize_labware(other_thread_plate)
    pad = Location("pad_1", pad_resource)
    my_plate = _make_labware("my_plate")
    manager = LocationReservationManager(_make_location_reg(pad))

    assert manager.can_reserve(
        "pad_1", thread_id="thread_A", requesting_labware_id=my_plate.id,
    ) is False


def test_can_reserve_rejects_when_no_labware_id_and_occupied() -> None:
    """Backward compat: callers that do not pass labware get the old
    "any-occupancy rejects" behavior, so pre-Round-5 callsites (move
    handler, ad-hoc reservation calls) keep their existing semantics
    until the full architectural fix routes labware everywhere.
    """
    pad_resource = PlatePad("pad_1")
    pad_resource.initialize_labware(_make_labware("some_plate"))
    pad = Location("pad_1", pad_resource)
    manager = LocationReservationManager(_make_location_reg(pad))

    assert manager.can_reserve("pad_1", thread_id="thread_A") is False


def test_can_reserve_grants_empty_location_regardless_of_labware_arg() -> None:
    """Empty location grants whether or not labware is provided."""
    pad = Location("pad_1", PlatePad("pad_1"))
    manager = LocationReservationManager(_make_location_reg(pad))

    assert manager.can_reserve("pad_1") is True
    assert manager.can_reserve(
        "pad_1", thread_id="thread_A", requesting_labware_id="any_id",
    ) is True


def test_can_reserve_still_blocks_when_different_thread_holds_reservation() -> None:
    """Reservation-holder exclusivity overrides labware-ownership grant.

    Even if the requester owns the labware sitting at the location, an
    existing reservation by a different thread blocks. Reservation
    holder exclusivity is the strictly enforced contract.
    """
    pad_resource = PlatePad("pad_1")
    my_plate = _make_labware("my_plate")
    pad_resource.initialize_labware(my_plate)
    pad = Location("pad_1", pad_resource)
    manager = LocationReservationManager(_make_location_reg(pad))

    held = LocationReservation(pad, None)
    manager._reserve("pad_1", held, thread_id="thread_B")

    assert manager.can_reserve(
        "pad_1", thread_id="thread_A", requesting_labware_id=my_plate.id,
    ) is False


@pytest.mark.asyncio
async def test_attempt_reservation_grants_when_request_carries_own_labware() -> None:
    """End-to-end through ``attempt_reservation``.

    ``request.labware`` carries the labware identity; ``attempt_reservation``
    extracts ``request.labware.id`` and forwards to ``can_reserve``. The
    full action-resolver chain plumbs the labware from
    ``ExecutingLabwareThread`` down to the request construction site.
    """
    pad_resource = PlatePad("pad_1")
    my_plate = _make_labware("my_plate")
    pad_resource.initialize_labware(my_plate)
    pad = Location("pad_1", pad_resource)
    manager = LocationReservationManager(_make_location_reg(pad))

    request = LocationReservation(pad, my_plate)
    await manager.attempt_reservation("pad_1", request, thread_id="thread_A")

    assert request.granted.is_set()
    assert not request.rejected.is_set()


@pytest.mark.asyncio
async def test_attempt_reservation_rejects_when_request_carries_different_labware() -> None:
    """End-to-end cross-thread case: different labware at target -> reject."""
    pad_resource = PlatePad("pad_1")
    other_plate = _make_labware("other_plate")
    pad_resource.initialize_labware(other_plate)
    pad = Location("pad_1", pad_resource)
    my_plate = _make_labware("my_plate")
    manager = LocationReservationManager(_make_location_reg(pad))

    request = LocationReservation(pad, my_plate)
    await manager.attempt_reservation("pad_1", request, thread_id="thread_A")

    assert request.rejected.is_set()
    assert not request.granted.is_set()
