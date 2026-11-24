"""A position the labware service cannot save reaches whoever caused the change. Nothing logs it and carries on.

Inside a device command it fails the command as not saved. Outside one, the operator's statement, the clear-all,
the discharge and the reuse-bind each raise to their caller. Every other holder is still written first, and one
plate's saves commit in the order the ledger took them.
"""

import asyncio

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.config import OrcaConfig
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.resource_models.command_effects import arm_pick, arm_place, device_move
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_directory import LabwareDirectory, LabwarePlacements
from orca.resource_models.labware_location_service import ArrivalMechanism, InMemoryLabwareLocationService
from orca.resource_models.labware_placement import (
    Arrival, IUnsavedPositions, LabwarePlacer, PositionNotSavedError, Relocation,
)
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.labware_service import LabwareService
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.state.placement import PlacementLedger
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import (
    memory_ops_store, memory_runtime, memory_unsaved_positions, memory_variable_store,
)
from tests.memory_activity import memory_unsaved_positions
from tests.test_helpers import (
    create_test_labware_instance, create_test_plate_template, create_test_transporter, no_forgetting, wire_system_map,
)

pytestmark = [pytest.mark.timeout(60), pytest.mark.positions_bound_by_the_build]


class _Database:
    """The labware service over an in-memory store, whose writes fail while it is down."""

    def __init__(self) -> None:
        self.store = InMemoryLabwareStore()
        self._service = LabwareService(self.store)
        self.down = False
        self.registered: list[tuple[str, str | None]] = []

    def _refuse_while_down(self) -> None:
        if self.down:
            raise RuntimeError("database is locked")

    async def register(self, instance: LabwareInstance, execution_id: str | None = None) -> None:
        self._refuse_while_down()
        await self._service.register(instance, execution_id)
        self.registered.append((instance.id, execution_id))

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        return await self._service.get_by_id(labware_id)

    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None:
        return await self._service.get_by_barcode(barcode)

    async def get_by_position(self, position_id: str) -> LabwareInstance | None:
        return await self._service.get_by_position(position_id)

    async def update_location(self, labware_id: str, position_id: str) -> None:
        self._refuse_while_down()
        await self._service.update_location(labware_id, position_id)

    async def clear_location(self, labware_id: str) -> None:
        self._refuse_while_down()
        await self._service.clear_location(labware_id)

    async def list_active_locations(self) -> list[tuple[str, str]]:
        return await self._service.list_active_locations()

    async def list_departed_locations(self) -> list[tuple[str, str]]:
        return await self._service.list_departed_locations()

    async def get_location_history(self, labware_id: str) -> list[tuple[str, str | None, float]]:
        return await self._service.get_location_history(labware_id)


async def _started(database: _Database) -> SystemRuntime:
    transporter = create_test_transporter("robot1", ["pad1", "pad2"])
    registry = ResourceRegistry()
    registry.add_resource(transporter)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, pads=["pad1", "pad2"])
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[create_test_plate_template("plate_96")],
        resources_registry=registry, system_map=system_map, workflows=[], event_bus=EventBus(),
        config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=database,
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    runtime = await memory_runtime(builder.get_system())
    await runtime.start()
    return runtime


async def _plate_on_pad1(runtime: SystemRuntime) -> LabwareInstance:
    snapshot = await runtime.labware.register("plate_96", location="pad1", confirm=True)
    return next(instance for instance in runtime.system.labwares if instance.id == snapshot.id)


async def _sent() -> None:
    return None


async def _recorded(_answer: None) -> None:
    return None


async def test_a_command_whose_position_cannot_be_saved_fails_as_not_saved() -> None:
    database = _Database()
    runtime = await _started(database)
    plate = await _plate_on_pad1(runtime)
    pad1, pad2 = runtime.system.system_map.get_location("pad1"), runtime.system.system_map.get_location("pad2")
    database.down = True

    with pytest.raises(CommandNotSavedError, match="database is locked"):
        await runtime.system.command_pipeline.run(
            "robot1", "move_plate", _sent, _recorded,
            statement=device_move("robot1", plate, pad1, pad2), checks=(), params=None,
            execution_id=None, events=None, carries_out=None,
        )

    assert runtime.system.labware_location_service.get(plate) is pad2, "the ledger follows the hardware"
    assert database.store.get_location(plate.id) == "pad1"
    await runtime.shutdown()


async def test_a_place_onto_a_pad_the_record_already_fills_writes_nothing_and_fails_as_not_saved() -> None:
    database = _Database()
    runtime = await _started(database)
    carried = await _plate_on_pad1(runtime)
    blocking = await runtime.labware.register("plate_96", location="pad2", confirm=True)
    robot = runtime.system.get_transporter("robot1")
    pad1, pad2 = runtime.system.system_map.get_location("pad1"), runtime.system.system_map.get_location("pad2")
    pipeline = runtime.system.command_pipeline
    await pipeline.run(
        "robot1", "pick_at_coords", _sent, _recorded,
        statement=arm_pick(robot, pad1, carried), checks=(), params=None,
        execution_id=None, events=None, carries_out=None,
    )

    with pytest.raises(CommandNotSavedError, match="pad2 already holds"):
        await pipeline.run(
            "robot1", "place_at_coords", _sent, _recorded,
            statement=arm_place(robot, pad2, carried, pad1), checks=(), params=None,
            execution_id=None, events=None, carries_out=None,
        )

    jaws = robot.gripper_location
    assert runtime.system.labware_location_service.get(carried) is jaws, "the record keeps the plate in the jaws"
    assert jaws.labware is carried
    assert pad2.labware is not None and pad2.labware.id == blocking.id
    assert database.store.get_location(carried.id) == jaws.position_id
    conflicts = await runtime.incidents.list(category=IncidentCategory.DECK_RECONCILE_CONFLICT)
    assert len(conflicts) == 1, f"got {[incident.message for incident in conflicts]}"
    assert carried.name in conflicts[0].message and blocking.name in conflicts[0].message
    assert f"still has '{carried.name}' in the jaws of 'robot1'" in conflicts[0].message
    await runtime.shutdown()


async def test_a_device_move_onto_a_pad_the_record_already_fills_tells_the_operator_it_is_still_at_its_start() -> None:
    database = _Database()
    runtime = await _started(database)
    moved = await _plate_on_pad1(runtime)
    blocking = await runtime.labware.register("plate_96", location="pad2", confirm=True)
    pad1, pad2 = runtime.system.system_map.get_location("pad1"), runtime.system.system_map.get_location("pad2")

    with pytest.raises(CommandNotSavedError):
        await runtime.system.command_pipeline.run(
            "robot1", "move_plate", _sent, _recorded,
            statement=device_move("robot1", moved, pad1, pad2), checks=(), params=None,
            execution_id=None, events=None, carries_out=None,
        )

    assert runtime.system.labware_location_service.get(moved) is pad1
    conflicts = await runtime.incidents.list(category=IncidentCategory.DECK_RECONCILE_CONFLICT)
    assert len(conflicts) == 1, f"got {[incident.message for incident in conflicts]}"
    assert f"still has '{moved.name}' at 'pad1', where the move started" in conflicts[0].message
    assert blocking.name in conflicts[0].message and "jaws" not in conflicts[0].message
    await runtime.shutdown()


async def test_an_operator_statement_that_cannot_be_saved_reaches_the_operator() -> None:
    database = _Database()
    runtime = await _started(database)
    plate = await _plate_on_pad1(runtime)
    database.down = True

    with pytest.raises(PositionNotSavedError, match="database is locked"):
        await runtime.labware.edit_location(plate.id, "pad2", confirm=True, reason="test")

    await runtime.shutdown()


async def test_a_clear_all_whose_saved_positions_cannot_be_cleared_reaches_the_operator() -> None:
    """Includes a plate only the database knows, which would otherwise come back on the next boot."""
    database = _Database()
    runtime = await _started(database)
    unknown_to_the_runtime = LabwareInstance("plate_96", "96_well")
    await database.register(unknown_to_the_runtime)
    await database.update_location(unknown_to_the_runtime.id, "pad2")
    database.down = True

    with pytest.raises(PositionNotSavedError, match="database is locked"):
        await runtime.labware.clear_all_labware(force=True, confirm=True, reason="test")

    assert database.store.get_location(unknown_to_the_runtime.id) == "pad2"
    await runtime.shutdown()


async def test_a_discharge_whose_saved_position_cannot_be_cleared_reaches_the_operator() -> None:
    database = _Database()
    runtime = await _started(database)
    plate = await _plate_on_pad1(runtime)
    database.down = True

    with pytest.raises(PositionNotSavedError, match="discharge it again"):
        await runtime.labware.discharge_labware(plate.id, force=True, confirm=True, reason="test")

    assert database.store.get_location(plate.id) == "pad1"
    assert all(instance.id != plate.id for instance in runtime.system.labwares), "the registry still lets it go"
    assert runtime.system.system_map.get_location("pad1").labware is None, "the slot still lets it go"
    await runtime.shutdown()


def _binding_workflow(runtime: SystemRuntime, database: _Database, start: Location) -> tuple[ExecutingWorkflow, MagicMock]:
    workflow = object.__new__(ExecutingWorkflow)
    workflow._labware_service = database
    workflow._system = runtime.system
    workflow._workflow = MagicMock(id="exec-bind")
    template = MagicMock()
    template.start_reuse_existing = True
    template.name = "plate_journey"
    template.start_location = start
    template.labware_template.name = "plate_96"
    template.labware_template.create_instance = AsyncMock(side_effect=lambda: LabwareInstance("plate_96", "96_well"))
    return workflow, template


async def test_a_reuse_bind_whose_position_cannot_be_saved_reaches_the_run() -> None:
    """A resident the database has at the start site is bound to its own id, and the position is saved again."""
    database = _Database()
    runtime = await _started(database)
    resident = LabwareInstance("plate_96", "96_well")
    await database.register(resident)
    await database.update_location(resident.id, "pad1")
    start = runtime.system.system_map.get_location("pad1")
    workflow, template = _binding_workflow(runtime, database, start)
    database.down = True

    with pytest.raises(PositionNotSavedError, match="database is locked"):
        await workflow._resolve_reuse_bind(template)

    await runtime.shutdown()


async def test_a_fresh_reuse_bind_saves_its_run_and_position_once() -> None:
    database = _Database()
    runtime = await _started(database)
    start = runtime.system.system_map.get_location("pad1")
    workflow, template = _binding_workflow(runtime, database, start)

    resolved = await workflow._resolve_reuse_bind(template)
    # Before the reads, so a write still in flight anywhere would be counted.
    await runtime.shutdown()

    assert resolved is not None and resolved.created_fresh
    bound = resolved.labware_instance
    assert bound is not None
    assert database.registered == [(bound.id, "exec-bind")]
    assert [row[0] for row in await database.get_location_history(bound.id)] == ["pad1"]
    assert database.store.get_location(bound.id) == "pad1"


class _Worlds:
    """The deck projection and the other run-mode worlds a placer is given, recording what they were told."""

    def __init__(self) -> None:
        self.projected: list[str] = []
        self.marked_stale = 0

    async def project(self, labware: LabwareInstance, *locations: Location) -> None:
        self.projected.append(labware.id)

    async def go_stale(self) -> None:
        self.marked_stale += 1


async def _no_rebuild(device_location: Location) -> None:
    return None


def _placer(
    database: _Database, worlds: _Worlds, unsaved: IUnsavedPositions | None = None,
) -> tuple[LabwarePlacer, InMemoryLabwareLocationService]:
    unsaved = unsaved or memory_unsaved_positions()
    # Its own ledger: this module's runtime tests bind positions through their build, not the per-test one.
    ledger = InMemoryLabwareLocationService(LabwarePlacements(PlacementLedger(), LabwareDirectory()))
    placer = LabwarePlacer(
        ledger, database, unsaved, worlds.project, worlds.go_stale, worlds.go_stale, _no_rebuild, no_forgetting,
    )
    return placer, ledger


def _pad(ledger: InMemoryLabwareLocationService, name: str) -> Location:
    pad = Location(name, resource=PlatePad(name))
    ledger.bind_placements(pad)
    return pad


async def test_a_clear_all_marks_every_world_so_a_holder_it_could_not_reach_is_rebuilt_later() -> None:
    database, worlds = _Database(), _Worlds()
    placer, _ledger = _placer(database, worlds)

    await placer.cleared_all([], [])

    assert worlds.marked_stale == 1


@pytest.mark.parametrize("how", [Relocation.STATED, Relocation.RESTATED])
async def test_a_relocation_that_cannot_be_saved_still_clears_the_other_worlds(how: Relocation) -> None:
    database, worlds = _Database(), _Worlds()
    placer, ledger = _placer(database, worlds)
    source, target = _pad(ledger, "pad1"), _pad(ledger, "pad2")
    plate = LabwareInstance("plate", "96_well")
    await source.place_labware(plate)
    ledger.update(plate, source)
    database.down = True

    with pytest.raises(PositionNotSavedError, match="database is locked"):
        await placer.relocate(plate, source, target, how)

    assert ledger.get(plate) is target
    assert worlds.marked_stale == 1, "a world that still holds it at pad1 refuses the next run there"


@pytest.mark.parametrize("how", [Arrival.STATED, Arrival.RESTATED])
async def test_a_stated_arrival_that_cannot_be_saved_still_clears_the_other_worlds(how: Arrival) -> None:
    database, worlds = _Database(), _Worlds()
    placer, ledger = _placer(database, worlds)
    pad = _pad(ledger, "pad1")
    plate = LabwareInstance("plate", "96_well")
    database.down = True

    with pytest.raises(PositionNotSavedError, match="database is locked"):
        await placer.arrived(plate, pad, how)

    assert ledger.get(plate) is pad
    assert worlds.marked_stale == 1


async def test_a_spawn_that_cannot_be_saved_still_tells_the_device() -> None:
    database, worlds = _Database(), _Worlds()
    placer, ledger = _placer(database, worlds)
    output = _pad(ledger, "output")
    plate = LabwareInstance("plate", "96_well")
    database.down = True

    with pytest.raises(PositionNotSavedError, match="database is locked"):
        await placer.arrived(plate, output, Arrival.SPAWNED)

    assert ledger.get(plate) is output
    assert worlds.projected == [plate.id], "the device deck is told the plate is there"


class _FirstMoveIsSlow(_Database):
    """The first saved position waits until the test lets it through, as a write behind a busy database does."""

    def __init__(self) -> None:
        super().__init__()
        self.first_waiting = asyncio.Event()
        self.let_first_through = asyncio.Event()
        self._moves = 0

    async def update_location(self, labware_id: str, position_id: str) -> None:
        self._moves += 1
        if self._moves == 1:
            self.first_waiting.set()
            await self.let_first_through.wait()
        await super().update_location(labware_id, position_id)


async def test_two_saves_of_one_plate_commit_in_the_order_the_ledger_took_them() -> None:
    database, worlds = _FirstMoveIsSlow(), _Worlds()
    placer, ledger = _placer(database, worlds)
    jaws, pad = _pad(ledger, "jaws"), _pad(ledger, "pad1")
    plate = LabwareInstance("plate", "96_well")

    picked = asyncio.create_task(placer.picked_up(plate, jaws))
    await database.first_waiting.wait()
    set_down = asyncio.create_task(placer.set_down(plate, pad, jaws))
    for _ in range(20):
        await asyncio.sleep(0)
    database.let_first_through.set()
    await asyncio.gather(picked, set_down)

    assert ledger.get(plate) is pad
    assert database.store.get_location(plate.id) == "pad1"
    assert [row[0] for row in await database.get_location_history(plate.id)] == ["jaws", "pad1"]


async def test_a_save_cancelled_while_it_waits_its_turn_does_not_hold_up_the_next() -> None:
    """A thread stopped while its save waits behind another must not leave every later save of that plate waiting."""
    database, worlds = _FirstMoveIsSlow(), _Worlds()
    placer, ledger = _placer(database, worlds)
    jaws, pad = _pad(ledger, "jaws"), _pad(ledger, "pad1")
    plate = LabwareInstance("plate", "96_well")

    picked = asyncio.create_task(placer.picked_up(plate, jaws))
    await database.first_waiting.wait()
    set_down = asyncio.create_task(placer.set_down(plate, pad, jaws))
    for _ in range(20):
        await asyncio.sleep(0)
    set_down.cancel()
    await asyncio.gather(set_down, return_exceptions=True)
    database.let_first_through.set()
    await picked

    await asyncio.wait_for(placer.confirmed(plate, pad), timeout=2)

    assert database.store.get_location(plate.id) == "pad1"


async def test_a_discharge_that_cannot_be_saved_still_takes_it_off_every_deck() -> None:
    """The discharge's two departures: the site's deck lets it go, then every deck in every world."""
    database, worlds = _Database(), _Worlds()
    placer, ledger = _placer(database, worlds)
    pad = _pad(ledger, "pad1")
    plate = LabwareInstance("plate", "96_well")
    await pad.place_labware(plate)
    ledger.update(plate, pad)
    database.down = True

    with pytest.raises(PositionNotSavedError, match="discharge it again"):
        await placer.left(plate, pad)
    with pytest.raises(PositionNotSavedError, match="discharge it again"):
        await placer.left(plate, None)

    assert pad.labware is None
    assert worlds.projected == [plate.id], "the site's device deck is told it left"
    assert worlds.marked_stale == 1, "every world is marked for a rebuild"


async def test_a_set_down_cancelled_while_it_waits_its_turn_still_saves_where_the_plate_is() -> None:
    """A thread stopped mid-move keeps the ledger's new position, so the database must get it too, or a restart
    brings the plate back in the jaws."""
    database, worlds = _FirstMoveIsSlow(), _Worlds()
    placer, ledger = _placer(database, worlds)
    jaws, pad = _pad(ledger, "jaws"), _pad(ledger, "pad1")
    plate = LabwareInstance("plate", "96_well")

    picked = asyncio.create_task(placer.picked_up(plate, jaws))
    await database.first_waiting.wait()
    set_down = asyncio.create_task(placer.set_down(plate, pad, jaws))
    for _ in range(20):
        await asyncio.sleep(0)
    set_down.cancel()
    with pytest.raises(asyncio.CancelledError):
        await set_down
    database.let_first_through.set()
    await picked

    async def saved_at_the_pad() -> None:
        while database.store.get_location(plate.id) != "pad1":
            await asyncio.sleep(0.01)

    await asyncio.wait_for(saved_at_the_pad(), timeout=2)
    assert ledger.get(plate) is pad
    assert [row[0] for row in await database.get_location_history(plate.id)] == ["jaws", "pad1"]


async def test_a_save_that_fails_after_its_caller_was_cancelled_is_reported() -> None:
    database, worlds = _FirstMoveIsSlow(), _Worlds()
    unsaved = memory_unsaved_positions()
    placer, ledger = _placer(database, worlds, unsaved)
    reported = unsaved.reported
    jaws, pad = _pad(ledger, "jaws"), _pad(ledger, "pad1")
    plate = LabwareInstance("plate", "96_well")

    picked = asyncio.create_task(placer.picked_up(plate, jaws))
    await database.first_waiting.wait()
    set_down = asyncio.create_task(placer.set_down(plate, pad, jaws))
    for _ in range(20):
        await asyncio.sleep(0)
    set_down.cancel()
    with pytest.raises(asyncio.CancelledError):
        await set_down
    database.down = True
    database.let_first_through.set()
    with pytest.raises(PositionNotSavedError):
        await picked

    async def reported_once() -> None:
        while not reported:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(reported_once(), timeout=2)
    assert [labware_id for labware_id, _ in reported] == [plate.id]


class _MovesWaitWhenAsked(_Database):
    """The next saved position waits on ``hold``, as a write behind a busy database does."""

    def __init__(self) -> None:
        super().__init__()
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()

    async def update_location(self, labware_id: str, position_id: str) -> None:
        hold, self.hold = self.hold, None
        if hold is not None:
            self.held.set()
            await hold.wait()
        await super().update_location(labware_id, position_id)


async def test_a_shutdown_returns_only_after_a_cancelled_moves_save_is_written() -> None:
    """The next build reads the database. A save still in flight when shutdown returns lands after it, and the
    database then disagrees with the record the build made."""
    database = _MovesWaitWhenAsked()
    runtime = await _started(database)
    plate = await _plate_on_pad1(runtime)
    placer = runtime.system.labware_placer
    jaws = runtime.system.transporters[0].gripper_location
    pad2 = runtime.system.system_map.get_location("pad2")
    release = asyncio.Event()
    database.hold = release

    picked = asyncio.create_task(placer.picked_up(plate, jaws))
    await database.held.wait()
    set_down = asyncio.create_task(placer.set_down(plate, pad2, jaws))
    for _ in range(20):
        await asyncio.sleep(0)
    set_down.cancel()
    with pytest.raises(asyncio.CancelledError):
        await set_down
    asyncio.get_running_loop().call_later(1.0, release.set)
    await runtime.shutdown()

    assert database.store.get_location(plate.id) == "pad2"
    await picked


async def test_a_save_still_running_past_the_shutdown_limit_is_reported() -> None:
    database, worlds = _FirstMoveIsSlow(), _Worlds()
    unsaved = memory_unsaved_positions()
    placer, ledger = _placer(database, worlds, unsaved)
    jaws = _pad(ledger, "jaws")
    plate = LabwareInstance("plate", "96_well")

    picked = asyncio.create_task(placer.picked_up(plate, jaws))
    await database.first_waiting.wait()
    await placer.finish_saves(0.05)

    assert [labware_id for labware_id, _ in unsaved.reported] == [plate.id]
    with pytest.raises(asyncio.CancelledError):
        await picked


async def _moved(runtime: SystemRuntime, plate: LabwareInstance, source: Location | None, target: Location) -> None:
    await runtime.system.command_pipeline.run(
        "robot1", "move_plate", _sent, _recorded,
        statement=device_move("robot1", plate, source, target), checks=(), params=None,
        execution_id=None, events=None, carries_out=None,
    )


async def test_a_plate_set_down_where_the_record_already_has_it_is_not_refused() -> None:
    database = _Database()
    runtime = await _started(database)
    plate = await _plate_on_pad1(runtime)
    pad1 = runtime.system.system_map.get_location("pad1")

    await _moved(runtime, plate, None, pad1)

    assert runtime.system.labware_location_service.get(plate) is pad1
    await runtime.shutdown()


async def test_a_pad_that_only_expects_another_plate_takes_a_set_down() -> None:
    database = _Database()
    runtime = await _started(database)
    plate = await _plate_on_pad1(runtime)
    pad1, pad2 = runtime.system.system_map.get_location("pad1"), runtime.system.system_map.get_location("pad2")
    expected = await create_test_labware_instance("expected_plate")
    runtime.system.labware_location_service.expect(expected, pad2, ArrivalMechanism.MANUAL_PLACE)

    await _moved(runtime, plate, pad1, pad2)

    assert runtime.system.labware_location_service.get(plate) is pad2
    assert database.store.get_location(plate.id) == "pad2"
    await runtime.shutdown()


async def test_a_pad_whose_last_plate_was_retired_takes_a_set_down() -> None:
    database = _Database()
    runtime = await _started(database)
    plate = await _plate_on_pad1(runtime)
    pad1, pad2 = runtime.system.system_map.get_location("pad1"), runtime.system.system_map.get_location("pad2")
    snapshot = await runtime.labware.register("plate_96", location="pad2", confirm=True)
    retired = next(instance for instance in runtime.system.labwares if instance.id == snapshot.id)
    assert runtime.system.labware_location_service.retire(retired)

    await _moved(runtime, plate, pad1, pad2)

    assert runtime.system.labware_location_service.get(plate) is pad2
    assert database.store.get_location(plate.id) == "pad2"
    await runtime.shutdown()
