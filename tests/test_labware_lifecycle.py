"""Characterization tests for Device/Location labware lifecycle and can_reserve().

These tests pin down the ACTUAL behavior of the labware tracking system
using real objects. They exist to prevent confusion about what
Location.labware, Device.labware, and can_reserve() return at each
stage of the pick/place lifecycle.

Key finding (single-occupancy ruling 2026-07-18): a device site holds ONE
plate. LabwareStagingBridge.labware reports the site's single occupant,
staged OR loaded -- a loaded plate keeps the site occupied. can_reserve()
checks location.labware, so a loaded plate blocks foreign reservations;
only a request carrying the occupant's own labware identity is granted.
"""

import pytest

from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.devices import Device
from orca.resource_models.position_occupancy import PositionOccupancy
from orca.state.placement import Reach
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from tests.test_helpers import wire_system_map
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import LocationReservationManager
from orca.system.system_map import SystemMap
from tests.mock import EXTERNAL_MOVER, UniversalMockDevice
from tests.test_helpers import create_test_labware_instance, create_test_transporter


# ---------------------------------------------------------------------------
# 1. Staged and loaded are one record at two reaches
# ---------------------------------------------------------------------------

class TestStagedVsLoaded:
    """The two-phase staging model, now a reach on one placement rather than a
    staged field and a loaded list that could disagree."""

    async def test_a_site_starts_empty(self) -> None:
        occupancy = PositionOccupancy("dev/slot")
        assert occupancy.labware is None
        assert occupancy.loaded_labware == []

    async def test_a_plate_at_the_approach_point_is_reachable(self) -> None:
        occupancy = PositionOccupancy("dev/slot")
        plate = await create_test_labware_instance("plate1")
        occupancy.arrive(plate)

        assert occupancy.accessible_labware is plate
        assert occupancy.inside_labware == []

    async def test_loading_clamps_it_inside_and_keeps_the_site_occupied(self) -> None:
        occupancy = PositionOccupancy("dev/slot")
        plate = await create_test_labware_instance("plate1")
        occupancy.arrive(plate)
        occupancy.reached(plate, Reach.INSIDE)

        assert occupancy.accessible_labware is None
        assert occupancy.inside_labware == [plate]
        assert occupancy.labware is plate

    async def test_staging_it_back_out_makes_it_reachable_again(self) -> None:
        occupancy = PositionOccupancy("dev/slot")
        plate = await create_test_labware_instance("plate1")
        occupancy.arrive(plate)
        occupancy.reached(plate, Reach.INSIDE)

        occupancy.reached(plate, Reach.AT_HAND)

        assert occupancy.accessible_labware is plate
        assert occupancy.inside_labware == []

    async def test_leaving_empties_the_site(self) -> None:
        occupancy = PositionOccupancy("dev/slot")
        plate = await create_test_labware_instance("plate1")
        occupancy.arrive(plate)
        occupancy.reached(plate, Reach.INSIDE)
        occupancy.reached(plate, Reach.AT_HAND)

        occupancy.leave(plate)

        assert occupancy.labware is None
        assert occupancy.loaded_labware == []

    async def test_a_second_plate_is_refused_while_one_is_inside(self) -> None:
        """A loaded plate leaves nothing at the approach point, which is what
        used to let a second plate be staged onto an occupied site."""
        occupancy = PositionOccupancy("dev/slot")
        first = await create_test_labware_instance("plate1")
        second = await create_test_labware_instance("plate2")
        occupancy.arrive(first)
        occupancy.reached(first, Reach.INSIDE)

        with pytest.raises(SlotOccupiedError):
            occupancy.arrive(second)


# ---------------------------------------------------------------------------
# 2. LabwareStagingBridge.labware returns staged, NOT loaded
# ---------------------------------------------------------------------------

class TestDeviceLabwareProperty:
    """LabwareStagingBridge.labware returns the site's single occupant (staged
    OR loaded). This is the property that Location.labware delegates to, and
    that can_reserve() checks."""

    def test_device_labware_is_none_initially(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        assert nest.labware is None
        assert nest.loaded_labware == []

    @pytest.mark.asyncio
    async def test_device_labware_after_place(self) -> None:
        """After notify_placed, the plate moves to loaded_labware and the site
        stays occupied: LabwareStagingBridge.labware reports the loaded plate
        (single occupancy -- 'loaded' is clamped-in-place, not a second slot)."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)

        assert nest.labware is plate
        assert plate in nest.loaded_labware

    @pytest.mark.asyncio
    async def test_device_labware_during_pick_preparation(self) -> None:
        """During prepare_for_pick, the plate moves from loaded to staged."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)
        await nest.prepare_for_pick(plate, EXTERNAL_MOVER)

        assert nest.labware is plate
        assert plate not in nest.loaded_labware

    @pytest.mark.asyncio
    async def test_device_labware_after_pick(self) -> None:
        """After notify_picked, both staged and loaded are empty."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)
        await nest.prepare_for_pick(plate, EXTERNAL_MOVER)
        await nest.notify_picked(plate, EXTERNAL_MOVER)

        assert nest.labware is None
        assert nest.loaded_labware == []


# ---------------------------------------------------------------------------
# 3. PlatePad.labware returns actual labware (no staging)
# ---------------------------------------------------------------------------

class TestPlatePadLabwareProperty:
    """PlatePad.labware tracks actual presence -- no staging indirection."""

    async def test_plate_pad_labware_after_initialize(self) -> None:
        pad = PlatePad("pad1")
        plate = await create_test_labware_instance("plate1")
        pad.initialize_labware(plate)

        assert pad.labware is plate

    @pytest.mark.asyncio
    async def test_plate_pad_labware_after_pick(self) -> None:
        pad = PlatePad("pad1")
        plate = await create_test_labware_instance("plate1")
        pad.initialize_labware(plate)

        await pad.prepare_for_pick(plate, EXTERNAL_MOVER)
        await pad.notify_picked(plate, EXTERNAL_MOVER)

        assert pad.labware is None

    @pytest.mark.asyncio
    async def test_plate_pad_labware_after_place(self) -> None:
        pad = PlatePad("pad1")
        plate = await create_test_labware_instance("plate1")

        await pad.prepare_for_place(plate, EXTERNAL_MOVER)
        await pad.notify_placed(plate, EXTERNAL_MOVER)

        assert pad.labware is plate


# ---------------------------------------------------------------------------
# 4. Location.labware delegates to resource.labware
# ---------------------------------------------------------------------------

class TestLocationLabware:
    """Location.labware returns whatever the underlying resource returns."""

    def test_location_with_device_returns_staged(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        location = Location("dev1", nest)

        assert location.labware is None  # nest staged is None

    @pytest.mark.asyncio
    async def test_location_with_device_after_place(self) -> None:
        """After placing a plate, Location.labware reports the loaded plate:
        the bridge's single-occupancy view keeps the site occupied."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        location = Location("dev1", nest)
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)

        assert location.labware is plate
        assert plate in nest.loaded_labware

    async def test_location_with_pad_returns_actual_labware(self) -> None:
        pad = PlatePad("pad1")
        plate = await create_test_labware_instance("plate1")
        pad.initialize_labware(plate)
        location = Location("pad1", pad)

        # PlatePad returns actual labware, not staged
        assert location.labware is plate


# ---------------------------------------------------------------------------
# 5. can_reserve() behavior with real Device locations
# ---------------------------------------------------------------------------

class TestCanReserveWithRealDevices:
    """can_reserve() checks location.labware, which under single occupancy
    reports the loaded plate too. A device site with a loaded plate is
    occupied and blocks anonymous reservations; only a request carrying the
    occupant's own labware identity is granted."""

    async def _build_system_map(self) -> tuple[SystemMap, LabwareStagingBridge, PlatePad]:
        from orca.sdk.system import ResourceRegistry
        device = UniversalMockDevice("dev1")
        transporter = create_test_transporter("robot1", ["dev1", "pad1"])

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)

        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"dev1": device}, pads=["pad1"])

        nest = system_map.get_location("dev1/slot").resource
        assert isinstance(nest, LabwareStagingBridge)
        pad = system_map.get_location("pad1").resource
        assert isinstance(pad, PlatePad)
        return system_map, nest, pad

    async def test_can_reserve_empty_device(self) -> None:
        system_map, nest, _ = await self._build_system_map()
        manager = LocationReservationManager(system_map)

        assert manager.can_reserve("dev1/slot") is True

    @pytest.mark.asyncio
    async def test_can_reserve_device_with_loaded_plate(self) -> None:
        """CRITICAL: can_reserve returns False when a plate is loaded --
        the loaded plate keeps the site occupied (single occupancy), so the
        occupancy gate blocks anonymous requesters. A request carrying the
        occupant's own labware identity is still granted."""
        system_map, nest, _ = await self._build_system_map()
        manager = LocationReservationManager(system_map)
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)
        assert plate in nest.loaded_labware

        assert manager.can_reserve("dev1/slot") is False
        assert manager.can_reserve("dev1/slot", requesting_labware_id=plate.id) is True

    @pytest.mark.asyncio
    async def test_can_reserve_device_during_pick_prep(self) -> None:
        """During prepare_for_pick, plate is on stage, so can_reserve is False."""
        system_map, nest, _ = await self._build_system_map()
        manager = LocationReservationManager(system_map)
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)
        await nest.prepare_for_pick(plate, EXTERNAL_MOVER)

        assert manager.can_reserve("dev1/slot") is False

    async def test_can_reserve_pad_with_labware(self) -> None:
        """PlatePad tracks actual labware, so can_reserve is False when occupied."""
        system_map, _, pad = await self._build_system_map()
        manager = LocationReservationManager(system_map)
        plate = await create_test_labware_instance("plate1")

        pad.initialize_labware(plate)

        assert manager.can_reserve("pad1") is False

    @pytest.mark.asyncio
    async def test_reservation_blocks_second_reserve(self) -> None:
        """Even if location.labware is None, an active reservation blocks."""
        system_map, device, _ = await self._build_system_map()
        manager = LocationReservationManager(system_map)

        location = system_map.get_location("dev1/slot")
        reservation = LocationReservation(location)
        manager._reserve("dev1/slot", reservation)

        assert manager.can_reserve("dev1/slot") is False

    @pytest.mark.asyncio
    async def test_release_then_reserve_device_with_loaded_plate(self) -> None:
        """After releasing a reservation, another thread can NOT reserve a
        device that has a loaded plate: the loaded plate itself occupies the
        site (single occupancy), closing the old BUG-1 safety net gap."""
        system_map, nest, _ = await self._build_system_map()
        manager = LocationReservationManager(system_map)
        plate = await create_test_labware_instance("plate1")

        # Place plate on device (goes to loaded; site stays occupied)
        await nest.notify_placed(plate, EXTERNAL_MOVER)

        # Thread 1 holds reservation
        location = system_map.get_location("dev1/slot")
        reservation1 = LocationReservation(location)
        manager._reserve("dev1/slot", reservation1)

        # Thread 1 releases
        manager.release_reservation("dev1/slot")

        # Thread 2 is still blocked by the loaded plate (occupancy gate)
        assert manager.can_reserve("dev1/slot") is False

        reservation2 = LocationReservation(location)
        await manager.attempt_reservation("dev1/slot", reservation2)
        assert reservation2.rejected.is_set()
        assert not reservation2.granted.is_set()


# ---------------------------------------------------------------------------
# 6. Asymmetry: Device vs PlatePad labware semantics
# ---------------------------------------------------------------------------

class TestDeviceVsPadAsymmetry:
    """Device and PlatePad track labware through different mechanisms
    (staging bridge vs direct), but under single occupancy both report the
    occupant via .labware after placement -- can_reserve() sees them alike."""

    @pytest.mark.asyncio
    async def test_pad_reports_occupied_after_place(self) -> None:
        """PlatePad.labware is NOT None after placement."""
        pad = PlatePad("pad1")
        plate = await create_test_labware_instance("plate1")
        await pad.notify_placed(plate, EXTERNAL_MOVER)

        assert pad.labware is plate  # actual tracking

    @pytest.mark.asyncio
    async def test_device_reports_occupied_after_place(self) -> None:
        """LabwareStagingBridge.labware reports the loaded plate after
        placement (single occupancy: loaded keeps the site occupied)."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        plate = await create_test_labware_instance("plate1")
        await nest.notify_placed(plate, EXTERNAL_MOVER)

        assert nest.labware is plate  # loaded occupant is visible
        assert plate in nest.loaded_labware


# ---------------------------------------------------------------------------
# 7. Error recovery scenarios: reservation behavior when plate is loaded
# ---------------------------------------------------------------------------

async def _build_system_with_device() -> tuple[SystemMap, LabwareStagingBridge, LocationReservationManager, UniversalMockDevice]:
    """Build a minimal system: one device, one pad, one transporter."""
    from orca.sdk.system import ResourceRegistry
    device = UniversalMockDevice("dev1")
    transporter = create_test_transporter("robot1", ["dev1", "pad1"])

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"dev1": device}, pads=["pad1"])
    nest = system_map.get_location("dev1/slot").resource
    assert isinstance(nest, LabwareStagingBridge)
    manager = LocationReservationManager(system_map)

    return system_map, nest, manager, device


class TestReservationDuringErrorRecovery:
    """These tests pin down what the reservation system sees during error
    recovery scenarios. The key insight: after a plate is placed and loaded,
    the site stays occupied (single occupancy), so the occupancy gate blocks
    foreign threads even when no reservation is held. Only a request carrying
    the occupant's own labware identity gets back in."""

    @pytest.mark.asyncio
    async def test_device_appears_occupied_to_reservation_system_after_placement(self) -> None:
        """After a plate is placed and loaded on a device, the reservation
        system sees the device as occupied: the loaded plate keeps the site
        occupied and can_reserve() rejects anonymous requesters."""
        system_map, nest, manager, device = await _build_system_with_device()
        await device.initialize()
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)

        assert plate in nest.loaded_labware
        assert nest.labware is plate
        assert manager.can_reserve("dev1/slot") is False

    @pytest.mark.asyncio
    async def test_releasing_reservation_does_not_expose_device_to_other_threads(self) -> None:
        """When a thread releases its reservation while a plate is loaded,
        another thread still cannot reserve: the loaded plate itself blocks
        via the occupancy gate. This is the secondary safety net that closed
        the old BUG-1 gap (releasing on pause no longer lets other threads
        claim the location)."""
        system_map, nest, manager, device = await _build_system_with_device()
        await device.initialize()
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)
        location = system_map.get_location("dev1/slot")

        # Thread A holds reservation
        reservation_a = LocationReservation(location)
        manager._reserve("dev1/slot", reservation_a)
        assert manager.can_reserve("dev1/slot") is False

        # Thread A releases (behavior on pause)
        manager.release_reservation("dev1/slot")

        # Thread B is still blocked by the loaded plate
        reservation_b = LocationReservation(location)
        await manager.attempt_reservation("dev1/slot", reservation_b)
        assert reservation_b.rejected.is_set()
        assert not reservation_b.granted.is_set()

    @pytest.mark.asyncio
    async def test_same_thread_can_reacquire_after_release(self) -> None:
        """After releasing, the same thread can re-reserve the device
        (RETRY release-then-reacquire). Works because the request carries the
        thread's own labware identity, which matches the site's occupant."""
        system_map, nest, manager, device = await _build_system_with_device()
        await device.initialize()
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)
        location = system_map.get_location("dev1/slot")

        reservation1 = LocationReservation(location, labware=plate)
        manager._reserve("dev1/slot", reservation1)
        manager.release_reservation("dev1/slot")

        reservation2 = LocationReservation(location, labware=plate)
        await manager.attempt_reservation("dev1/slot", reservation2)
        assert reservation2.granted.is_set()

    @pytest.mark.asyncio
    async def test_held_reservation_blocks_even_when_device_appears_empty(self) -> None:
        """An active reservation blocks other threads regardless of
        Device.labware state. The reservation is what protects against
        concurrent access when a plate is loaded."""
        system_map, nest, manager, device = await _build_system_with_device()
        await device.initialize()
        plate = await create_test_labware_instance("plate1")

        await nest.notify_placed(plate, EXTERNAL_MOVER)
        location = system_map.get_location("dev1/slot")

        reservation_a = LocationReservation(location)
        manager._reserve("dev1/slot", reservation_a)

        # Blocked by reservation, not by labware
        assert manager.can_reserve("dev1/slot") is False

    @pytest.mark.asyncio
    async def test_pad_vs_device_reservation_symmetry(self) -> None:
        """PlatePad.labware tracks actual presence and, under single
        occupancy, so does the device site: can_reserve() blocks on both
        when a plate is present. No reliance on the reservation alone."""
        system_map, nest, manager, device = await _build_system_with_device()
        await device.initialize()
        plate = await create_test_labware_instance("plate1")

        # Device with loaded plate: can_reserve sees occupied
        await nest.notify_placed(plate, EXTERNAL_MOVER)
        assert manager.can_reserve("dev1/slot") is False

        # Pad with plate: can_reserve sees occupied
        pad = system_map.get_location("pad1").resource
        assert isinstance(pad, PlatePad)
        pad.initialize_labware(await create_test_labware_instance("plate2"))
        assert manager.can_reserve("pad1") is False
