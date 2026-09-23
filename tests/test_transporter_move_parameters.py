"""The transporter resolves a move's scalars and sends them with the request."""

import pytest
from pydantic import ValidationError

from cheshire_drivers.move_parameters import (
    SEED_MOVE_PARAMETERS,
    MoveParameterPatch,
)
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.transporter import Transporter
from orca.runtime.move_parameters import move_defaults_for_device
from orca.runtime.db import create_memory_engine
from orca.runtime.move_defaults_service import MoveDefaultsService
from orca.runtime.sqlite_move_defaults_store import SqliteMoveDefaultsStore
from orca.runtime.sqlite_teachpoint_store import SqliteTeachpointStore
from orca.runtime.teachpoint_service import TeachpointService


def _teachpoint(access: AccessConfig | None = None) -> Teachpoint:
    if access is None:
        access = AccessConfig(
            name="default_vertical", access_type="vertical",
            gripper_offset=20.0, vertical_clearance=20.0, horizontal_clearance=100.0,
        )
    return Teachpoint(
        position_id="pad_1",
        coordinates=CartesianCoordinates(x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0),
        orientation="left",
        access=access,
    )


async def _transporter(defaults: MoveParameterPatch | None = None) -> Transporter:
    teachpoints = TeachpointService(SqliteTeachpointStore(create_memory_engine()))
    move_defaults = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    if defaults is not None:
        await move_defaults.set("pf400", defaults)
    transporter = Transporter("pf400", teachpoint_store=teachpoints)
    transporter.bind_move_defaults(move_defaults)
    return transporter


@pytest.mark.asyncio
async def test_a_transporter_with_no_stored_defaults_still_moves() -> None:
    """Nothing configured has to work, or a fresh deployment cannot move a plate."""
    transporter = await _transporter()

    resolved = await transporter.resolve_handling(_teachpoint())

    assert resolved.parameters == SEED_MOVE_PARAMETERS


@pytest.mark.asyncio
async def test_the_stored_defaults_are_what_an_unconfigured_move_gets() -> None:
    transporter = await _transporter(defaults=MoveParameterPatch(travel_margin=25.0))

    resolved = await transporter.resolve_handling(_teachpoint())

    assert resolved.parameters.travel_margin == 25.0
    assert resolved.sources["travel_margin"] == "defaults"


@pytest.mark.asyncio
async def test_a_field_nobody_tuned_says_it_came_from_the_seed() -> None:
    """Tuning one number must not make the other nine look chosen, or nobody can
    tell a measured value from one that has never been looked at."""
    transporter = await _transporter(defaults=MoveParameterPatch(travel_margin=25.0))

    resolved = await transporter.resolve_handling(_teachpoint())

    assert resolved.sources["travel_margin"] == "defaults"
    assert resolved.sources["jaw_opening"] == "seed"
    assert resolved.parameters.jaw_opening == SEED_MOVE_PARAMETERS.jaw_opening


@pytest.mark.asyncio
async def test_the_defaults_are_per_transporter_not_per_deployment() -> None:
    """A second arm has its own safe margins; one shared row would be wrong."""
    move_defaults = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    await move_defaults.set("some_other_arm", MoveParameterPatch(travel_margin=25.0))
    transporter = Transporter(
        "pf400",
        teachpoint_store=TeachpointService(SqliteTeachpointStore(create_memory_engine())),
    )
    transporter.bind_move_defaults(move_defaults)

    resolved = await transporter.resolve_handling(_teachpoint())

    assert resolved.parameters.travel_margin == SEED_MOVE_PARAMETERS.travel_margin


@pytest.mark.asyncio
async def test_the_site_narrows_the_stored_defaults() -> None:
    transporter = await _transporter()
    tight_nest = AccessConfig(
        name="tight", access_type="vertical",
        gripper_offset=3.0, vertical_clearance=45.0, horizontal_clearance=100.0,
    )

    resolved = await transporter.resolve_handling(_teachpoint(tight_nest))

    assert resolved.parameters.clearance == 45.0
    assert resolved.sources["clearance"] == "site"


@pytest.mark.asyncio
async def test_a_stored_row_survives_a_round_trip_through_the_database() -> None:
    store = SqliteMoveDefaultsStore(create_memory_engine())
    tuned = MoveParameterPatch(jaw_opening=18.0, speed=20.0)

    await store.set("pf400", tuned)

    assert await store.get("pf400") == tuned


@pytest.mark.asyncio
async def test_a_row_naming_a_field_the_model_lost_fails_on_read() -> None:
    """Half a record must not reach an arm: a field removed from the model makes
    every row that names it unreadable rather than silently dropping it."""
    from sqlalchemy import update

    from orca.runtime.db import make_session_factory
    from orca.runtime.db.models import MoveDefaultsRow

    engine = create_memory_engine()
    store = SqliteMoveDefaultsStore(engine)
    await store.set("pf400", MoveParameterPatch(jaw_opening=18.0))
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        await session.execute(
            update(MoveDefaultsRow)
            .where(MoveDefaultsRow.transporter_name == "pf400")
            .values(patch={"grip_height": 4.0}),
        )
        await session.commit()

    with pytest.raises(ValidationError):
        await store.get("pf400")


@pytest.mark.asyncio
async def test_the_row_stores_only_the_fields_somebody_set() -> None:
    """A total record would freeze the untouched fields at whatever the seed said
    the day the row was written, so a later seed correction would never reach the
    arms that never disagreed with it."""
    store = SqliteMoveDefaultsStore(create_memory_engine())

    await store.set("pf400", MoveParameterPatch(jaw_opening=18.0))

    stored = await store.get("pf400")
    assert stored is not None
    assert stored.model_dump(exclude_none=True) == {"jaw_opening": 18.0}



@pytest.mark.asyncio
async def test_a_started_runtime_binds_every_transporter_to_the_stored_defaults() -> None:
    """A topology that never mentions move defaults must still read the stored row.

    Leaving the wiring to the topology author is how a deployment ends up quietly
    running on the seed while an operator edits a row nothing ever consults.
    """
    from orca.runtime.system_runtime import SystemRuntime

    from tests.test_system_runtime import _build_simple_system

    system, _ = await _build_simple_system()
    service = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    await service.set("robot1", MoveParameterPatch(travel_margin=25.0))

    runtime = SystemRuntime(system, move_defaults_service=service)
    await runtime.start()
    try:
        transporter = system.transporters[0]
        resolved = await transporter.resolve_handling(_teachpoint())
    finally:
        await runtime.shutdown(confirm=True)

    assert resolved.parameters.travel_margin == 25.0


@pytest.mark.asyncio
async def test_the_daemon_runtime_reads_the_store_the_operator_writes() -> None:
    """The daemon holds one store factory precisely so the registries an operator
    edits and the runtime that dispatches never diverge. A runtime that mints its
    own store instead would read an empty table and silently use the seed, which
    looks exactly like the feature working.
    """
    from orca.runtime.deployment_registries import build_in_memory_deployment_layer

    factory, registries = build_in_memory_deployment_layer()

    await registries.move_defaults.apply(
        "robot1", MoveParameterPatch(travel_margin=25.0), confirm=True,
    )

    assert await factory.move_defaults().get("robot1") == MoveParameterPatch(
        travel_margin=25.0,
    )


@pytest.mark.asyncio
async def test_an_operator_edit_replaces_the_row_rather_than_adding_one() -> None:
    """The update branch IS the operator edit path; a second row would make which
    one wins a matter of query order."""
    store = SqliteMoveDefaultsStore(create_memory_engine())
    await store.set("pf400", MoveParameterPatch(jaw_opening=14.0))
    tuned = MoveParameterPatch(jaw_opening=18.0)

    await store.set("pf400", tuned)

    assert await store.get("pf400") == tuned
    assert list(await store.list()) == ["pf400"]


@pytest.mark.asyncio
async def test_deleting_a_row_puts_the_transporter_back_on_the_seed() -> None:
    store = SqliteMoveDefaultsStore(create_memory_engine())
    await store.set("pf400", MoveParameterPatch(speed=20.0))

    assert await store.delete("pf400") is True
    assert await store.get("pf400") is None
    assert await store.delete("pf400") is False


@pytest.mark.asyncio
async def test_a_pick_sends_the_resolved_record_to_the_driver() -> None:
    """Resolving correctly and then not sending it is the same as not resolving."""
    from unittest.mock import AsyncMock, patch

    from orca.resource_models.location import Location

    teachpoints = TeachpointService(SqliteTeachpointStore(create_memory_engine()))
    await teachpoints.add(_teachpoint())
    move_defaults = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    await move_defaults.set("pf400", MoveParameterPatch(jaw_opening=18.0))
    transporter = Transporter("pf400", teachpoint_store=teachpoints)
    transporter.bind_move_defaults(move_defaults)

    location = Location("pad_1")
    # A real instance rather than a stub: a stub silently stops matching the
    # moment a pick reads one more field off the labware.
    plate = LabwareInstance("plate_tmpl", "Cor_96_wellplate_360ul_Fb")

    with patch.object(type(location), "labware", property(lambda _self: plate)),             patch.object(transporter, "_sim_manager") as manager:
        manager.driver = AsyncMock()
        await transporter._do_pick(location)
        request = manager.driver.pick_at_coords.call_args.args[0]

    assert request.handling.jaw_opening == 18.0
    assert request.handling.clearance == 20.0


@pytest.mark.asyncio
async def test_a_pick_sends_what_this_one_plate_asked_to_be_carried_with() -> None:
    """Layer four is only worth anything if it reaches the arm. Resolving it and
    dropping it before the wire is the same as never having it."""
    from unittest.mock import AsyncMock, patch

    from orca.resource_models.location import Location

    teachpoints = TeachpointService(SqliteTeachpointStore(create_memory_engine()))
    await teachpoints.add(_teachpoint())
    move_defaults = MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
    await move_defaults.set("pf400", MoveParameterPatch(jaw_opening=18.0))
    transporter = Transporter("pf400", teachpoint_store=teachpoints)
    transporter.bind_move_defaults(move_defaults)

    location = Location("pad_1")
    plate = LabwareInstance("plate_tmpl", "Cor_96_wellplate_360ul_Fb")
    plate.carry_with(jaw_opening=25.0, speed=30.0)

    with patch.object(type(location), "labware", property(lambda _self: plate)),             patch.object(transporter, "_sim_manager") as manager:
        manager.driver = AsyncMock()
        await transporter._do_pick(location)
        request = manager.driver.pick_at_coords.call_args.args[0]

    assert request.handling.jaw_opening == 25.0
    assert request.handling.speed == 30.0


class _SystemWith:
    """The slice of a system `move_defaults_for_device` reads."""

    def __init__(self, name: str, resource: object) -> None:
        self._name = name
        self._resource = resource

    def has_resource(self, name: str) -> bool:
        return name == self._name

    def get_resource(self, name: str) -> object:
        return self._resource


@pytest.mark.asyncio
async def test_a_bare_gripper_command_reads_the_same_row_a_pick_reads() -> None:
    """An open names no teachpoint, so nothing narrows it, but it still has to open
    by the number a pick opens by. Resolving it anywhere else leaves the operator's
    button on one opening and the pick on another."""
    transporter = await _transporter(MoveParameterPatch(jaw_opening=18.0))

    defaults = await move_defaults_for_device(_SystemWith("pf400", transporter), "pf400")

    assert defaults.jaw_opening == 18.0


@pytest.mark.asyncio
async def test_a_gripper_command_at_an_unmodelled_device_gets_the_seed() -> None:
    """Teaching happens before a topology is mounted, so a name the system does not
    know still answers rather than refusing."""
    defaults = await move_defaults_for_device(None, "pf400")

    assert defaults == SEED_MOVE_PARAMETERS
