"""A move picks the labware it was planned for, or it picks nothing.

A move's source is fixed when the path is planned, which can be minutes or hours
before the reservation is granted. Anything can happen at that slot in between:
an operator moves the plate by hand, or puts a different one down. The pick took
whatever the slot held, so a second plate at the source got carried away under
the first one's name, and the ledger recorded the wrong labware in the gripper.
"""

from unittest.mock import Mock

import pytest

from cheshire_drivers.sims import SimTransporterValidationError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import PlacementState
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.move_action import (
    ExecutableMoveAction,
    LabwareNotAtSourceError,
    MoveAction,
)
from tests.test_helpers import (
    no_source_hold,
    make_labware_placer,
    create_test_labware_instance,
    create_test_transporter,
)


def _status_manager() -> Mock:
    sm = Mock()
    sm.set_status = Mock()
    return sm


def _thread_context() -> Mock:
    ctx = Mock()
    ctx.execution_id = "exec_1"
    ctx.workflow_name = "wf"
    ctx.thread_id = "thread_1"
    ctx.thread_name = "thread1"
    ctx.template_name = "tmpl_1"
    return ctx


async def _build_move(
    *,
    source: Location,
    target: Location,
    transporter: Transporter,
    labware_location_service: Mock,
) -> tuple[ExecutableMoveAction, LabwareInstance]:
    labware = await create_test_labware_instance("plate_1")
    move = MoveAction(labware, source, target, transporter)
    reservation = LocationReservation(requested_location=target, labware=labware)
    reservation.set_location(target)
    move.set_reservation(reservation)
    executing = ExecutableMoveAction(
        status_manager=_status_manager(),
        context=_thread_context(),
        action=move,
        labware_location_service=labware_location_service,
        labware_placer=make_labware_placer(labware_location_service),
        slot_holder=no_source_hold(),
    )
    return executing, labware


def _service_placing_at(location: Location | None) -> Mock:
    service = Mock()
    service.placement.return_value = (
        PlacementState.PRESENT if location is not None else None
    )
    service.get.return_value = location
    return service


@pytest.mark.asyncio
async def test_a_move_refuses_when_another_plate_is_sitting_at_its_source() -> None:
    """The dangerous case: the slot is occupied, just not by our labware.

    An empty slot already refused. A slot holding something else did not: the
    pick took the occupant, and the move went on to record OUR plate as the one
    in the gripper, so two labware were wrong at once.
    """
    source = Location("source_loc", resource=PlatePad("source_pad"))
    target = Location("target_loc", resource=PlatePad("target_pad"))
    transporter = create_test_transporter("robot1", ["source_loc", "target_loc"])
    elsewhere = Location("bench_loc", resource=PlatePad("bench_pad"))

    executing, labware = await _build_move(
        source=source, target=target, transporter=transporter,
        labware_location_service=_service_placing_at(elsewhere),
    )
    stranger = await create_test_labware_instance("plate_2")
    await source.place_labware(stranger)

    with pytest.raises(LabwareNotAtSourceError) as exc:
        await executing.execute()

    assert labware.name in str(exc.value)
    assert stranger.name in str(exc.value), "name what is actually in the way"
    assert "bench_loc" in str(exc.value), "and where the ledger has ours"
    assert transporter.labware is None, "nothing may be picked"
    assert source.labware is stranger, "and the stranger stays where it is"


@pytest.mark.asyncio
async def test_the_refusal_says_where_the_labware_went_when_the_source_is_empty(
) -> None:
    """An empty source already raised, but said nothing an operator could act on."""
    source = Location("source_loc", resource=PlatePad("source_pad"))
    target = Location("target_loc", resource=PlatePad("target_pad"))
    transporter = create_test_transporter("robot1", ["source_loc", "target_loc"])
    elsewhere = Location("bench_loc", resource=PlatePad("bench_pad"))

    executing, labware = await _build_move(
        source=source, target=target, transporter=transporter,
        labware_location_service=_service_placing_at(elsewhere),
    )

    with pytest.raises(LabwareNotAtSourceError) as exc:
        await executing.execute()

    assert "bench_loc" in str(exc.value)
    assert labware.name in str(exc.value)


@pytest.mark.asyncio
async def test_a_retry_that_already_holds_the_labware_reaches_the_place() -> None:
    """A failed place leaves the plate in the jaws and the source empty.

    That retry skips the pick entirely, so a source check that fired on it
    would wedge the one recovery path an operator has. The sim transporter's
    own world is not seeded in this unit harness, so the place then fails on
    its own account -- reaching it at all is what this pins.
    """
    source = Location("source_loc", resource=PlatePad("source_pad"))
    target = Location("target_loc", resource=PlatePad("target_pad"))
    transporter = create_test_transporter("robot1", ["source_loc", "target_loc"])

    executing, labware = await _build_move(
        source=source, target=target, transporter=transporter,
        labware_location_service=_service_placing_at(
            transporter.gripper_location
        ),
    )
    await transporter.gripper_location.place_labware(labware)

    with pytest.raises(SimTransporterValidationError):
        await executing.execute()
