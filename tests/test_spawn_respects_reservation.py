"""Spawn placement must respect location reservations.

The move path reserves its destination location and holds that reservation
across pick + transit + place. During transit the reserving plate is off the
deck, so the destination slot is momentarily *physically* empty while still
*reserved*. A spawn that placed on physical emptiness alone would drop a
different plate into that slot; the reserving move then collides on arrival
(``ValueError: <location> already contains labware``) and the execution
deadlocks -- labware placed onto a location another thread holds exclusively.

These guard the reservation layer directly (deterministic, no full workflow):
a spawn waits until it can hold the target location itself, then places, exactly
as ``MoveHandler.acquire_placement_reservation`` promises. A free location still
places immediately (no spurious wait).
"""

from unittest.mock import Mock

import asyncio
import pytest

from orca.config import ReservationConfig
from orca.devices.devices import Storage
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.resource_models.labware_location_service import (
    InMemoryLabwareLocationService,
)
from orca.workflow_models.spawn_actions import DispenseSpawn, ManualPlaceSpawn


def _location(name: str = "pad1") -> Location:
    return Location(name, resource=PlatePad(name))


def _registry(location: Location) -> Mock:
    reg = Mock()
    reg.get_location.return_value = location
    return reg


def _move_handler(location: Location) -> MoveHandler:
    coordinator = ThreadReservationCoordinator(_registry(location), Mock())
    return MoveHandler(
        coordinator,
        Mock(),
        Mock(),
        ReservationConfig(retry_interval=0.05, move_reservation_timeout=5.0),
    )


def _spawning_thread(location: Location) -> tuple[LabwareThreadInstance, LabwareInstance]:
    plate = LabwareInstance("plate_96", "96_well")
    thread = LabwareThreadInstance(
        labware=plate,
        start_location=location,
        end_locations=[location],
        run_mode=WorkflowRunMode.PURE_SIM,
    )
    return thread, plate


class _GatedAcquirer:
    """A ``PlacementReservationAcquirer`` the test drives: it records that the
    spawn reached the reservation step and holds it there until ``allow()``,
    proving the spawn reserves BEFORE it writes the slot rather than racing a
    plate onto a location another thread has reserved."""

    def __init__(self, reservation: LocationReservation) -> None:
        self._reservation = reservation
        self.acquiring = asyncio.Event()
        self._allowed = asyncio.Event()

    async def acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location
    ) -> LocationReservation:
        self.acquiring.set()
        await self._allowed.wait()
        return self._reservation

    def allow(self) -> None:
        self._allowed.set()


@pytest.mark.asyncio
async def test_spawn_reserves_before_placing() -> None:
    """The spawn acquires the location reservation BEFORE writing the slot and
    does not place until it is granted, so it cannot drop a plate onto a location
    another thread has reserved and is mid-transit toward. The block is proven by
    waiting on the acquire step as a condition (no wall-clock guess): the spawn must
    reach the reservation and hold there with the slot still empty."""
    location = _location()
    acquirer = _GatedAcquirer(LocationReservation(location))
    thread, plate = _spawning_thread(location)
    spawn = ManualPlaceSpawn(location, InMemoryLabwareLocationService(), acquirer)

    task = asyncio.create_task(spawn.acquire(thread))
    await asyncio.wait_for(acquirer.acquiring.wait(), timeout=2.0)
    assert location.labware is None, (
        "the spawn must acquire the reservation before it writes the slot"
    )
    assert not task.done()

    acquirer.allow()
    await asyncio.wait_for(task, timeout=2.0)
    assert location.labware is plate, (
        "the spawn places once the reservation is granted"
    )


@pytest.mark.asyncio
async def test_spawn_places_immediately_on_free_location() -> None:
    """No reservation, empty slot: the spawn places without waiting (the acquire
    grants on the first attempt, so wrapping placement in a reservation adds no
    spurious delay)."""
    location = _location()
    move_handler = _move_handler(location)

    thread, plate = _spawning_thread(location)
    spawn = ManualPlaceSpawn(location, InMemoryLabwareLocationService(), move_handler)

    await asyncio.wait_for(spawn.acquire(thread), timeout=2.0)
    assert location.labware is plate
    # The placement reservation is released after the plate is down; physical
    # occupancy now guards the slot.
    assert move_handler._thread_reservation_coordinator.get_active_reservations() == []


class _RecordingStorage(Storage):
    """A Storage whose dispense() only records that it ran, so a test can assert
    the physical dispense is gated behind the placement reservation."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.dispensed: list[str] = []

    async def dispense(self) -> None:
        self.dispensed.append("dispense")


@pytest.mark.asyncio
async def test_dispense_spawn_reserves_before_dispensing() -> None:
    """DispenseSpawn must hold the reservation across the PHYSICAL dispense, not
    only the slot write: dispensing advances a plate into the output, so a move
    that has reserved the location mid-transit must block the dispense too. The
    dispense must not fire until the reservation is granted."""
    storage = _RecordingStorage("stacker_1")
    location = Location("stacker_1")
    location._resource = LabwareStagingBridge("stacker_1", storage)

    acquirer = _GatedAcquirer(LocationReservation(location))
    thread, plate = _spawning_thread(location)
    spawn = DispenseSpawn(location, InMemoryLabwareLocationService(), acquirer)

    task = asyncio.create_task(spawn.acquire(thread))
    await asyncio.wait_for(acquirer.acquiring.wait(), timeout=2.0)
    assert storage.dispensed == [], (
        "DispenseSpawn must not dispense before the reservation is granted"
    )
    assert location.labware is None
    assert not task.done()

    acquirer.allow()
    await asyncio.wait_for(task, timeout=2.0)
    assert storage.dispensed == ["dispense"]
    assert location.labware is plate
