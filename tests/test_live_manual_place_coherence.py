"""A LIVE manual place leaves every holder that already knows the thread correct.

This file replaces a set of characterization tests, one per store, each added
after a bench run found another place still holding a labware that had been
swapped out from under it: the per-execution registry, the cached location
history, the status registry, a frozen action slot. They existed because a
thread's id IS its labware's id and the labware used to change identity when
the operator placed something.

It does not any more. `labware_register` adopts the expectation the thread is
already holding, so there is one instance for the whole life of the thread and
nothing to re-point. What is worth pinning now is the pair of things that made
the old design leak:

  * nothing about a labware that has not arrived reaches anything durable, and
  * a wait that ends without an arrival leaves nothing behind.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orca.resource_models.labware_placement import LabwarePlacer
from tests.test_helpers import command_pipeline_over, no_forgetting
from orca.resource_models.labware import LabwareInstance
from tests.placements_for_tests import this_tests_placements
from tests.thread_builder import build_executing_thread
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from orca.resource_models.labware_location_service import (
    InMemoryLabwareLocationService,
    PlacementState,
)
from orca.workflow_models.labware_state import InMemoryLabwareRegistry, LabwareState
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.spawn_actions import ManualPlaceSpawn
from orca.workflow_models.status_manager import StatusManager


def _make_platepad_location(name: str = "pad1") -> Location:
    return Location(name, resource=PlatePad(name))


def _make_thread(
    labware: LabwareInstance, start_location: Location,
) -> LabwareThreadInstance:
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=start_location,
        end_locations=[start_location],
        run_mode=WorkflowRunMode.LIVE,
    )
    template = MagicMock(name="plate_x")
    template.name = "plate_x"
    thread.set_labware_template(template)
    return thread


class _Projection:
    """The deck projection the placer is given, recording each labware it was asked about and where."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Location, ...]]] = []

    async def __call__(self, labware: LabwareInstance, *locations: Location) -> None:
        self.calls.append((labware.id, locations))


async def _no_rebuild(device_location: Location) -> None:
    return None


async def _no_staling() -> None:
    return None


def _make_executing(
    thread: LabwareThreadInstance,
    location_service: InMemoryLabwareLocationService,
    status_manager: StatusManager | None = None,
    acquirer: MagicMock | None = None,
    projection: _Projection | None = None,
) -> ExecutingLabwareThread:
    context = MagicMock()
    context.execution_id = "exec-live-place"
    context.workflow_name = "wf-live-place"
    return build_executing_thread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=acquirer if acquirer is not None else _granting_acquirer(),
        status_manager=status_manager if status_manager is not None else MagicMock(),
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=location_service,
        command_pipeline=command_pipeline_over(LabwarePlacer(
            location_service, memory_labware_service(), memory_unsaved_positions(), projection if projection is not None else _Projection(), _no_staling,
            _no_staling, _no_rebuild, no_forgetting,
        )),
    )


def _granting_acquirer() -> MagicMock:
    """A placement-reservation acquirer that grants immediately, standing in
    for the MoveHandler the engine passes. Both the blocking claim the sim
    spawn takes and the single-shot one a LIVE operator wait holds."""
    acquirer = MagicMock()
    acquirer.acquire_placement_reservation = AsyncMock(return_value=MagicMock())
    acquirer.try_acquire_placement_reservation = AsyncMock(return_value=MagicMock())
    return acquirer


def _operator_places(
    location: Location,
    labware: LabwareInstance,
    service: InMemoryLabwareLocationService,
) -> bool:
    """What `labware_register` does once it has adopted the expectation: write
    the slot, then record the arrival. True when the placer would save it."""
    location.initialize_labware(labware)
    return service.update(labware, location)


async def test_the_registry_entry_the_thread_registered_is_the_one_it_runs_with() -> None:
    """The C1 regression, restated: completion updates state by
    `thread.labware.id`, and that id has to still be in the registry."""
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    thread = _make_thread(labware, location)
    service = InMemoryLabwareLocationService(this_tests_placements())
    et = _make_executing(thread, service)
    registry = InMemoryLabwareRegistry()
    et.set_labware_registry(registry)

    _operator_places(location, labware, service)
    await et.initialize_labware()

    assert registry.get_state(thread.labware.id) == LabwareState.IN_JOURNEY
    # The exact call `_handle_thread_completion` makes.
    registry.update_state(thread.labware.id, LabwareState.ENDED)
    assert registry.get_state(labware.id) == LabwareState.ENDED


async def test_an_expected_labware_reaches_nothing_durable() -> None:
    """The placer saves a position only when the ledger records an arrival,
    and a saved position IS a real plate. A plate nobody has placed
    yet must not be one, or a reboot re-places it and blocks the slot."""
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())

    thread = _make_thread(labware, location)
    et = _make_executing(thread, service)
    et.set_labware_registry(InMemoryLabwareRegistry())

    assert service.placement(labware) is PlacementState.EXPECTED
    assert len(service.get_history(labware)) == 0

    assert _operator_places(location, labware, service) is True
    await et.initialize_labware()

    assert service.placement(labware) is PlacementState.PRESENT


async def test_a_wait_that_never_ends_in_an_arrival_leaves_nothing_behind() -> None:
    """Stopped before the operator placed anything. The identity was minted so
    the thread had something to name; with no thread it is not labware anybody
    should be shown."""
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())
    dropped: list[str] = []
    service.add_expectation_dropped_listener(lambda lw: dropped.append(lw.id))

    thread = _make_thread(labware, location)
    et = _make_executing(thread, service, status_manager=StatusManager(MagicMock()))
    et.set_labware_registry(InMemoryLabwareRegistry())
    et.publish_initial_status()

    parked = asyncio.create_task(et.initialize_labware())
    await asyncio.sleep(0)
    assert not parked.done()
    et.stop()
    await parked

    assert dropped == [labware.id]
    with pytest.raises(KeyError):
        service.placement(labware)


async def test_a_coordinator_that_raises_does_not_end_the_wait() -> None:
    """Claiming the spot is best effort; waiting for the plate is not.

    The operator is at the deck with the plate in hand. Ending their wait
    because the reservation manager raised leaves them holding it with nothing
    to put it into.
    """
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())
    thread = _make_thread(labware, location)
    acquirer = _granting_acquirer()
    acquirer.try_acquire_placement_reservation = AsyncMock(
        side_effect=RuntimeError("the reservation manager is having a day"),
    )
    et = _make_executing(thread, service, acquirer=acquirer)
    et.set_labware_registry(InMemoryLabwareRegistry())

    parked = asyncio.create_task(et.initialize_labware())
    await asyncio.sleep(0)
    assert not parked.done()

    _operator_places(location, labware, service)
    await asyncio.wait_for(parked, timeout=5)

    assert acquirer.try_acquire_placement_reservation.await_count >= 1
    assert service.placement(labware) is PlacementState.PRESENT


async def test_the_status_key_never_moves() -> None:
    """A thread's id IS its labware's id. The old swap moved that key, and every
    surface filing anything under it had to be told. Nothing moves now."""
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())
    emitted: list[str] = []
    event_bus = MagicMock()
    event_bus.emit.side_effect = lambda name, context: emitted.append(name)

    thread = _make_thread(labware, location)
    started_as = thread.id
    status_manager = StatusManager(event_bus)
    et = _make_executing(thread, service, status_manager=status_manager)
    et.set_labware_registry(InMemoryLabwareRegistry())

    _operator_places(location, labware, service)
    await et.initialize_labware()

    assert thread.id == started_as
    # AWAITING_MANUAL_PLACE on the bus IS the request for a place. The plate is
    # already on the pad, so the thread uses it there and asks for nothing.
    places = [name for name in emitted if name.endswith(".AWAITING_MANUAL_PLACE")]
    assert places == [], emitted


async def test_a_spawn_that_places_nothing_fails_the_thread_instead_of_running_on() -> None:
    """The sim shape of the same defect: a spawn that writes the slot without
    recording the arrival would leave the driver deck never told about this
    plate. Skipping the projection quietly turns that into an unexplained pick
    minutes later, so the thread fails here, naming the spawn."""
    from orca.workflow_models.errors import SpawnDidNotPlaceError

    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())
    thread = _make_thread(labware, location)
    et = _make_executing(thread, service, status_manager=StatusManager(MagicMock()))
    et.set_labware_registry(InMemoryLabwareRegistry())
    et.publish_initial_status()

    # A spawn that returns without ever recording an arrival.
    async def _places_nothing(_self: object, _thread: object) -> None:
        return None

    with patch.object(ManualPlaceSpawn, "acquire", _places_nothing):
        with pytest.raises(SpawnDidNotPlaceError) as exc_info:
            await et.initialize_labware()

    assert exc_info.value.labware_name == labware.name
    assert exc_info.value.placement == "EXPECTED"


@pytest.mark.parametrize(
    "run_mode", [WorkflowRunMode.CHECK_RUN, WorkflowRunMode.SIM],
)
async def test_a_sim_spawn_records_the_arrival_and_projects_the_deck(
    run_mode: WorkflowRunMode,
) -> None:
    """Sim writes the slot directly rather than through the placement
    chokepoint, so it has to record the arrival itself. When it did not, every
    sim thread's first action failed resolving its own location."""
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=location,
        end_locations=[location],
        run_mode=run_mode,
    )
    template = MagicMock(name="plate_x")
    template.name = "plate_x"
    thread.set_labware_template(template)
    projection = _Projection()
    et = _make_executing(thread, service, status_manager=StatusManager(MagicMock()), projection=projection)
    et._move_handler = _granting_acquirer()
    et.set_labware_registry(InMemoryLabwareRegistry())

    assert service.placement(labware) is PlacementState.EXPECTED
    await et.initialize_labware()

    assert location.labware is labware
    assert service.placement(labware) is PlacementState.PRESENT
    assert et.current_location is location
    assert [labware_id for labware_id, _ in projection.calls] == [labware.id], "the LH deck must learn about the plate"


async def test_an_expected_labware_never_reaches_a_slot_or_a_device() -> None:
    """The invariant every device projection rests on.

    `reconcile_lh_deck_occupancy` builds a liquid handler's deck from
    `Location.labware` -- the physical slot -- and the router, the reservation
    gate and the deadlock detector read the same thing. An expectation writes
    the position LEDGER and nothing else, so a plate nobody has put down cannot
    appear on a sim device's deck, cannot make the router treat an empty pad as
    blocked, and cannot be counted as a deadlock blocker. Putting it in the
    slot instead would hand all four a plate that is not there.
    """
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())
    thread = _make_thread(labware, location)
    projection = _Projection()
    et = _make_executing(thread, service, status_manager=StatusManager(MagicMock()), projection=projection)
    et.set_labware_registry(InMemoryLabwareRegistry())
    et.publish_initial_status()

    parked = asyncio.create_task(et.initialize_labware())
    await asyncio.sleep(0)

    assert service.placement(labware) is PlacementState.EXPECTED
    assert location.labware is None, "an expectation must not claim the slot"
    assert location.loaded_labware == []
    assert projection.calls == []

    et.stop()
    await parked


@pytest.mark.parametrize(
    "run_mode", [WorkflowRunMode.CHECK_RUN, WorkflowRunMode.SIM],
)
async def test_a_stop_that_lands_after_the_plate_did_still_projects_it(
    run_mode: WorkflowRunMode,
) -> None:
    """A sim place and a dispense never consult the stop event, so both can
    finish while a stop is already pending. The plate is on the slot at that
    point, and skipping the projection would leave the driver deck never
    told about it -- surfacing later as a pick of a plate the deck does not
    have."""
    location = _make_platepad_location()
    labware = LabwareInstance("plate_x", "96_well")
    service = InMemoryLabwareLocationService(this_tests_placements())
    thread = LabwareThreadInstance(
        labware=labware,
        start_location=location,
        end_locations=[location],
        run_mode=run_mode,
    )
    template = MagicMock(name="plate_x")
    template.name = "plate_x"
    thread.set_labware_template(template)
    projection = _Projection()
    et = _make_executing(thread, service, status_manager=StatusManager(MagicMock()), projection=projection)
    et._move_handler = _granting_acquirer()
    et.set_labware_registry(InMemoryLabwareRegistry())
    et.publish_initial_status()

    et.stop()
    await et.initialize_labware()

    assert location.labware is labware
    assert service.placement(labware) is PlacementState.PRESENT
    assert projection.calls == [(labware.id, (location,))]
