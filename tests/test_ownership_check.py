"""Cutover C3: the can_reserve ownership check.

A device-owned site rejects a foreign thread's labware while the owning
device's mutex is held by another thread's action, unless the labware is a
live member of that action. The holder's own thread and system locations
fall through to the existing occupancy outcomes untouched.
"""
from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import LocationReservationManager
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap


class _Owner:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name


async def _map_with_device_site() -> SystemMap:
    system_map = SystemMap(ResourceRegistry())
    mutex = Location("flex")
    system_map.register_mutex_location(mutex)
    site = DeckSiteLocation("flex/C2-slot", _Owner("flex"), mutex_position_id="flex")
    await system_map.add_site_location(site)
    await system_map.add_location(Location("pad_1"))
    return system_map


def _hold_mutex(
    manager: LocationReservationManager,
    system_map: SystemMap,
    thread_id: str,
    member_ids: set[str],
) -> None:
    holder = LocationReservation(system_map.get_location("flex"))
    holder.set_membership(lambda labware_id: labware_id in member_ids)
    manager._reserve("flex", holder, thread_id=thread_id)


async def test_foreign_labware_rejected_while_owner_mutex_held() -> None:
    system_map = await _map_with_device_site()
    manager = LocationReservationManager(system_map)
    _hold_mutex(manager, system_map, "owner-thread", member_ids=set())

    assert manager.can_reserve(
        "flex/C2-slot", thread_id="intruder-thread", requesting_labware_id="lw-x"
    ) is False


async def test_holder_thread_falls_through_to_occupancy() -> None:
    system_map = await _map_with_device_site()
    manager = LocationReservationManager(system_map)
    _hold_mutex(manager, system_map, "owner-thread", member_ids=set())

    assert manager.can_reserve(
        "flex/C2-slot", thread_id="owner-thread", requesting_labware_id="lw-own"
    ) is True


async def test_live_member_labware_is_sanctioned() -> None:
    system_map = await _map_with_device_site()
    manager = LocationReservationManager(system_map)
    _hold_mutex(manager, system_map, "owner-thread", member_ids={"lw-refill"})

    assert manager.can_reserve(
        "flex/C2-slot", thread_id="rack-thread", requesting_labware_id="lw-refill"
    ) is True
    assert manager.can_reserve(
        "flex/C2-slot", thread_id="rack-thread", requesting_labware_id="lw-other"
    ) is False


async def test_idle_device_site_grants_on_occupancy() -> None:
    system_map = await _map_with_device_site()
    manager = LocationReservationManager(system_map)

    assert manager.can_reserve(
        "flex/C2-slot", thread_id="any-thread", requesting_labware_id="lw-x"
    ) is True


async def test_system_location_skips_the_ownership_block() -> None:
    system_map = await _map_with_device_site()
    manager = LocationReservationManager(system_map)
    _hold_mutex(manager, system_map, "owner-thread", member_ids=set())

    assert manager.can_reserve(
        "pad_1", thread_id="intruder-thread", requesting_labware_id="lw-x"
    ) is True


async def test_loaded_bridge_site_refuses_a_second_reservation() -> None:
    """Single occupancy (owner ruling 2026-07-18): a plate clamped/loaded
    into a single-slot device still occupies its site, so can_reserve
    refuses a second plate for the whole staged+loaded span."""
    from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
    from tests.mock import EXTERNAL_MOVER, UniversalMockDevice
    from tests.test_helpers import create_test_labware_instance

    device = UniversalMockDevice("shaker1")
    bridge = LabwareStagingBridge("shaker1/slot", device)
    site = DeckSiteLocation(
        "shaker1/slot", _Owner("shaker1"), resource=bridge,
        mutex_position_id="shaker1",
    )
    system_map = SystemMap(ResourceRegistry())
    await system_map.add_site_location(site)
    manager = LocationReservationManager(system_map)

    plate = await create_test_labware_instance("plate_1")
    await site.notify_placed(plate, EXTERNAL_MOVER)

    assert manager.can_reserve(
        "shaker1/slot", thread_id="t2", requesting_labware_id="other-lw"
    ) is False, "a loaded plate must keep the site unreservable"

    await site.prepare_for_pick(plate, EXTERNAL_MOVER)
    await site.notify_picked(plate, EXTERNAL_MOVER)
    assert manager.can_reserve(
        "shaker1/slot", thread_id="t2", requesting_labware_id="other-lw"
    ) is True, "departure frees the site"
