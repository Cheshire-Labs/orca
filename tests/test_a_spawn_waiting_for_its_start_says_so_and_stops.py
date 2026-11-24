"""A spawn that has to wait for its start location says it is waiting, and a stop ends the wait.

Before this, a spawn blocked on an occupied start, a busy stacker output or a held reservation
retried every half second with the thread still reading CREATED. The stall detector counts CREATED
as progress, so the wait was invisible and nothing but a process restart ended it.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import ArrivalMechanism, InMemoryLabwareLocationService
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location
from orca.devices.devices import Storage
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.stall_detector import StallDetector, ThreadStallSnapshot, is_in_flight
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent, ThreadStateMachine
from orca.workflow_models.spawn_actions import DispenseSpawn, ManualPlaceSpawn, PlacementReservationAcquirer, SpawnWaitHooks
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.mock import EXTERNAL_MOVER, outside_any_system
from tests.placements_for_tests import this_tests_placements
from tests.test_helpers import make_labware_placer, wait_until


class _Hooks:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def as_hooks(self) -> SpawnWaitHooks:
        return SpawnWaitHooks(blocked=lambda: self.calls.append("blocked"), cleared=lambda: self.calls.append("cleared"))


class _NeverGrants:
    async def acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location,
    ) -> LocationReservation:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def try_acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location,
    ) -> LocationReservation | None:
        return None


def _pad(name: str = "pad1") -> Location:
    return Location(name, resource=PlatePad(name))


def _stoppable_thread(location: Location) -> tuple[LabwareThreadInstance, asyncio.Event]:
    plate = LabwareInstance("plate_96", "96_well")
    thread = LabwareThreadInstance(
        labware=plate, start_location=location, end_locations=[location], run_mode=WorkflowRunMode.CHECK_RUN,
    )
    stop = asyncio.Event()
    thread.set_stop_event(stop)
    return thread, stop


def _ledger(thread: LabwareThreadInstance, location: Location) -> InMemoryLabwareLocationService:
    service = InMemoryLabwareLocationService(this_tests_placements())
    service.expect(thread.labware, location, ArrivalMechanism.MANUAL_PLACE)
    return service


def _manual_place(
    thread: LabwareThreadInstance, location: Location, hooks: _Hooks, acquirer: PlacementReservationAcquirer | None = None,
) -> ManualPlaceSpawn:
    ledger = _ledger(thread, location)
    return ManualPlaceSpawn(location, ledger, make_labware_placer(ledger), acquirer, wait_hooks=hooks.as_hooks())


def _dispense(thread: LabwareThreadInstance, location: Location, hooks: _Hooks) -> DispenseSpawn:
    return DispenseSpawn(location, make_labware_placer(_ledger(thread, location)), wait_hooks=hooks.as_hooks())


@pytest.mark.asyncio
async def test_an_occupied_start_reports_blocked_then_cleared() -> None:
    location = _pad()
    prior = LabwareInstance("prior_plate", "96_well")
    location.initialize_labware(prior)
    thread, _ = _stoppable_thread(location)
    hooks = _Hooks()
    task = asyncio.create_task(_manual_place(thread, location, hooks).acquire(thread))

    await wait_until(lambda: hooks.calls == ["blocked"], timeout=5.0)

    await location.notify_picked(prior, EXTERNAL_MOVER)
    await asyncio.wait_for(task, timeout=5.0)
    assert hooks.calls == ["blocked", "cleared"]
    assert location.labware is thread.labware


@pytest.mark.asyncio
async def test_a_stop_ends_the_wait_for_an_occupied_start() -> None:
    location = _pad()
    prior = LabwareInstance("prior_plate", "96_well")
    location.initialize_labware(prior)
    thread, stop = _stoppable_thread(location)
    hooks = _Hooks()
    task = asyncio.create_task(_manual_place(thread, location, hooks).acquire(thread))

    await wait_until(lambda: hooks.calls == ["blocked"], timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert location.labware is prior


@pytest.mark.asyncio
async def test_a_stop_ends_the_wait_for_a_reservation_that_is_never_granted() -> None:
    location = _pad()
    thread, stop = _stoppable_thread(location)
    hooks = _Hooks()
    spawn = _manual_place(thread, location, hooks, _NeverGrants())
    task = asyncio.create_task(spawn.acquire(thread))

    await wait_until(lambda: hooks.calls == ["blocked"], timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert location.labware is None


@pytest.mark.asyncio
async def test_a_stop_ends_the_wait_for_a_busy_stacker_output() -> None:
    storage = outside_any_system(Storage("stacker_1"))
    location = Location("stacker_1")
    location._resource = LabwareStagingBridge("stacker_1", storage)
    location.initialize_labware(LabwareInstance("prior_plate", "96_well"))
    thread, stop = _stoppable_thread(location)
    hooks = _Hooks()
    task = asyncio.create_task(_dispense(thread, location, hooks).acquire(thread))

    await wait_until(lambda: hooks.calls == ["blocked"], timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


def test_waiting_for_the_start_location_is_a_state_and_returns_to_created() -> None:
    machine = ThreadStateMachine()
    assert machine.transition(ThreadEvent.START_LOCATION_AWAITED) is LabwareThreadStatus.AWAITING_START_LOCATION
    assert machine.transition(ThreadEvent.START_LOCATION_CLEARED) is LabwareThreadStatus.CREATED


def test_the_stall_detector_does_not_count_a_start_location_wait_as_progress() -> None:
    detector = StallDetector(required_stable_ticks=1)
    snapshots = [
        ThreadStallSnapshot(thread_id="t1", status=LabwareThreadStatus.AWAITING_CO_THREADS),
        ThreadStallSnapshot(thread_id="t2", status=LabwareThreadStatus.AWAITING_START_LOCATION),
    ]
    assert not is_in_flight(LabwareThreadStatus.AWAITING_START_LOCATION)
    assert detector.evaluate(snapshots, system_quiescent=True) is not None


@pytest.mark.asyncio
async def test_a_plate_already_dispensed_is_recorded_even_when_a_stop_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the stacker has pushed the plate out it exists, so the spawn records it rather than stop."""
    storage = outside_any_system(Storage("stacker_1"))
    location = Location("stacker_1")
    location._resource = LabwareStagingBridge("stacker_1", storage)
    thread, stop = _stoppable_thread(location)
    straggler = LabwareInstance("straggler", "96_well")

    async def dispense_into_a_busy_output() -> None:
        location.initialize_labware(straggler)
        stop.set()

    monkeypatch.setattr(storage, "dispense", dispense_into_a_busy_output)
    # The pick below only clears the output; no client is connected to take the stacker's own commands.
    monkeypatch.setattr(storage, "_do_notify_picked", AsyncMock())
    hooks = _Hooks()
    placing = asyncio.create_task(
        _dispense(thread, location, hooks).acquire(thread),
    )
    await wait_until(lambda: hooks.calls == ["blocked"], timeout=5.0)
    await location.notify_picked(straggler, EXTERNAL_MOVER)
    await asyncio.wait_for(placing, timeout=5.0)
    assert location.labware is thread.labware


class _GrantsWhenAllowed:
    """Grants the placement only after `allow` is set; records whether the request was given up."""

    def __init__(self) -> None:
        self.allow = asyncio.Event()
        self.cancelled = False
        self.granted: list[LocationReservation] = []

    async def acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location,
    ) -> LocationReservation:
        try:
            await self.allow.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        reservation = LocationReservation(location, labware)
        self.granted.append(reservation)
        return reservation

    async def try_acquire_placement_reservation(
        self, thread_id: str, labware: LabwareInstance, location: Location,
    ) -> LocationReservation | None:
        return None


@pytest.mark.asyncio
async def test_a_cancelled_spawn_gives_up_its_reservation_request() -> None:
    """An aborted run cancels the thread; the request it was waiting on must not outlive it and take the slot."""
    location = _pad()
    thread, _ = _stoppable_thread(location)
    hooks = _Hooks()
    acquirer = _GrantsWhenAllowed()
    task = asyncio.create_task(_manual_place(thread, location, hooks, acquirer).acquire(thread))

    await wait_until(lambda: hooks.calls == ["blocked"], timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert acquirer.cancelled
    assert acquirer.granted == []


@pytest.mark.asyncio
async def test_a_stopped_spawn_gives_up_its_reservation_request() -> None:
    """A stop ends the wait; the request must end with it, or it takes the slot once it frees and never lets go."""
    location = _pad()
    thread, stop = _stoppable_thread(location)
    hooks = _Hooks()
    acquirer = _GrantsWhenAllowed()
    task = asyncio.create_task(_manual_place(thread, location, hooks, acquirer).acquire(thread))

    await wait_until(lambda: hooks.calls == ["blocked"], timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)

    assert acquirer.cancelled
    assert acquirer.granted == []
