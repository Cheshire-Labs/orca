from tests.mock import EXTERNAL_MOVER
"""ManualPlaceSpawn unit tests.

`ManualPlaceSpawn` is the start-side strategy for both bare-string
`start="loc"` and explicit `start=("loc", MANUAL_PLACE)`. Both worlds run
the same transition -- the thread's labware goes from expected at the
start_location to present there -- and only the cause differs:

- `PURE_SIM` / `DEVICE_SIM`: the engine writes the slot immediately (retry
  on `DeviceBusyError`, same loop the deleted `DefaultSpawn` ran).
- `LIVE`: an operator puts the labware down and registers it. Register
  adopts the expectation this thread is holding, so the instance the thread
  started with is the one that arrives and nothing is swapped.

These tests exercise the strategy directly. The engine's
`AWAITING_MANUAL_PLACE` status emission belongs to `initialize_labware`.
"""

import asyncio

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import (
    ArrivalMechanism,
    InMemoryLabwareLocationService,
    PlacementState,
)
from orca.resource_models.location import ILabwareLocationObserver, Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.spawn_actions import ManualPlaceSpawn


def _make_platepad_location(name: str = "pad1") -> Location:
    pad = PlatePad(name)
    return Location(name, resource=pad)


def _fresh_labware(template_name: str = "plate_96") -> LabwareInstance:
    return LabwareInstance(template_name, "96_well")


def _make_thread(
    labware: LabwareInstance,
    start_location: Location,
    *,
    run_mode: WorkflowRunMode,
) -> LabwareThreadInstance:
    return LabwareThreadInstance(
        labware=labware,
        start_location=start_location,
        end_locations=[start_location],
        run_mode=run_mode,
    )


def _ledger_expecting(
    labware: LabwareInstance, location: Location,
) -> InMemoryLabwareLocationService:
    """A ledger in the state the thread constructor leaves it: the labware
    belongs at the location and has not arrived."""
    service = InMemoryLabwareLocationService()
    service.expect(labware, location, ArrivalMechanism.MANUAL_PLACE)
    return service


class _RecordingObserver(ILabwareLocationObserver):
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def notify_labware_location_change(
        self, event: str, location: Location, labware: LabwareInstance,
    ) -> None:
        self.events.append((event, location.name, labware.name))


class TestManualPlaceSpawnSimModes:
    """Sim modes auto-fulfill exactly like the deleted DefaultSpawn:
    write the factory's pre-fabricated labware into the slot, fire
    observers, never wait for an operator."""

    async def test_pure_sim_writes_slot_and_notifies_observers(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        observer = _RecordingObserver()
        location.add_observer(observer)

        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.PURE_SIM)
        spawn = ManualPlaceSpawn(location, _ledger_expecting(labware, location))
        await spawn.acquire(thread)

        assert location.labware is labware
        assert thread.labware is labware
        assert len(observer.events) == 1
        event, loc_name, _ = observer.events[0]
        assert event == "initialized"
        assert loc_name == "pad1"

    async def test_device_sim_writes_slot(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()

        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.DEVICE_SIM)
        spawn = ManualPlaceSpawn(location, _ledger_expecting(labware, location))
        await spawn.acquire(thread)

        assert location.labware is labware

    async def test_sim_waits_for_occupied_slot_to_clear(self) -> None:
        """Slot occupied by a prior thread's labware: PURE_SIM waits
        until the slot clears (same shape as the deleted DefaultSpawn's
        retry-on-busy)."""
        location = _make_platepad_location()
        prior_labware = _fresh_labware("prior_plate")
        location.initialize_labware(prior_labware)

        fresh = _fresh_labware()
        thread = _make_thread(fresh, location, run_mode=WorkflowRunMode.PURE_SIM)
        spawn = ManualPlaceSpawn(location, _ledger_expecting(fresh, location))
        acquire_task = asyncio.create_task(spawn.acquire(thread))

        await asyncio.sleep(0)
        assert not acquire_task.done()
        assert location.labware is prior_labware

        await location.notify_picked(prior_labware, EXTERNAL_MOVER)
        await acquire_task

        assert location.labware is fresh


class TestManualPlaceSpawnLive:
    """LIVE waits for the operator to place the labware the thread already
    holds. `labware_register` adopts the expectation rather than minting a
    second instance, so the wait ends on an arrival, not on a swap."""

    async def test_live_waits_until_its_own_labware_arrives(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.LIVE)
        service = _ledger_expecting(labware, location)
        spawn = ManualPlaceSpawn(location, service)

        acquire_task = asyncio.create_task(spawn.acquire(thread))
        await asyncio.sleep(0)
        assert not acquire_task.done()
        assert service.placement(labware) is PlacementState.EXPECTED

        # What `labware_register` does once it has adopted the expectation.
        location.initialize_labware(labware)
        service.update(labware, location)

        await acquire_task
        assert service.placement(labware) is PlacementState.PRESENT

    async def test_the_thread_keeps_the_identity_it_started_with(self) -> None:
        """No swap: the id every store cached at thread construction is still
        the id the thread runs against, so nothing needs re-pointing."""
        location = _make_platepad_location()
        labware = _fresh_labware()
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.LIVE)
        service = _ledger_expecting(labware, location)
        started_as = thread.id

        acquire_task = asyncio.create_task(
            ManualPlaceSpawn(location, service).acquire(thread)
        )
        await asyncio.sleep(0)
        location.initialize_labware(labware)
        service.update(labware, location)
        await acquire_task

        assert thread.labware is labware
        assert thread.id == started_as

    async def test_another_labware_landing_in_the_slot_does_not_end_the_wait(
        self,
    ) -> None:
        """Two threads waiting on one pad must not both take the first plate.
        The wait is on this thread's own labware, not on the slot filling."""
        location = _make_platepad_location()
        mine = _fresh_labware()
        someone_elses = _fresh_labware()  # same template
        thread = _make_thread(mine, location, run_mode=WorkflowRunMode.LIVE)
        service = _ledger_expecting(mine, location)
        service.expect(someone_elses, location, ArrivalMechanism.MANUAL_PLACE)

        acquire_task = asyncio.create_task(
            ManualPlaceSpawn(location, service).acquire(thread)
        )
        await asyncio.sleep(0)

        location.initialize_labware(someone_elses)
        service.update(someone_elses, location)
        await asyncio.sleep(0)
        assert not acquire_task.done()

        await location.notify_picked(someone_elses, EXTERNAL_MOVER)
        location.initialize_labware(mine)
        service.update(mine, location)
        await acquire_task

        assert thread.labware is mine
