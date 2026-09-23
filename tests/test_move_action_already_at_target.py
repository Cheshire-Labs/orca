"""A move whose labware already reached the target has nothing to actuate.

Both witnesses are covered: the target itself holding the labware (a place that
landed and then failed on the call after it, so the ledger is behind), and the
ledger reporting the target (an operator who finished the move by hand and
recorded it). Each test leaves the SOURCE empty, so any attempt to actuate
raises out of ``TransporterBase.pick`` -- completing proves nothing was driven.
"""

from unittest.mock import Mock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import PlacementState
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.move_action import (
    ExecutableMoveAction,
    MoveAction,
)
from orca.workflow_models.status_enums import ActionStatus
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


@pytest.mark.asyncio
async def test_move_completes_when_the_target_already_holds_its_labware() -> None:
    """A place can land and then fail on the call after it, which leaves the
    target holding the plate while the ledger still says the gripper. The move
    is done; treating its own delivery as a collision is what left an operator
    with no working verb."""
    source = Location("source_loc", resource=PlatePad("source_pad"))
    target = Location("target_loc", resource=PlatePad("target_pad"))
    transporter = create_test_transporter("robot1", ["source_loc", "target_loc"])

    service = Mock()
    service.placement.return_value = PlacementState.PRESENT
    service.get.return_value = transporter.gripper_location

    executing, labware = await _build_move(
        source=source, target=target, transporter=transporter,
        labware_location_service=service,
    )
    await target.place_labware(labware)

    await executing.execute()

    assert executing.status is ActionStatus.COMPLETED
    assert target.labware is labware
    assert transporter.labware is None


@pytest.mark.asyncio
async def test_move_completes_when_the_ledger_names_the_target_by_position_id() -> None:
    """The ledger's Location need not be the same object as the move's target.
    The location service itself treats two Locations sharing a position id as
    one place, so comparing by identity would re-drive the arm at a target the
    operator has already filled."""
    source = Location("source_loc", resource=PlatePad("source_pad"))
    target = Location("target_loc", resource=PlatePad("target_pad"))
    transporter = create_test_transporter("robot1", ["source_loc", "target_loc"])

    service = Mock()
    service.placement.return_value = PlacementState.PRESENT
    service.get.return_value = Location("target_loc", resource=PlatePad("rebuilt_pad"))

    executing, _ = await _build_move(
        source=source, target=target, transporter=transporter,
        labware_location_service=service,
    )

    await executing.execute()

    assert executing.status is ActionStatus.COMPLETED
    assert transporter.labware is None


@pytest.mark.asyncio
async def test_move_still_refuses_a_target_a_different_labware_occupies() -> None:
    """The occupancy refusal is not softened, only narrowed: a foreign plate on
    the target still blocks, and the message names it."""
    source = Location("source_loc", resource=PlatePad("source_pad"))
    target = Location("target_loc", resource=PlatePad("target_pad"))
    transporter = create_test_transporter("robot1", ["source_loc", "target_loc"])

    service = Mock()
    service.placement.return_value = PlacementState.PRESENT
    service.get.return_value = source

    executing, _ = await _build_move(
        source=source, target=target, transporter=transporter,
        labware_location_service=service,
    )
    intruder = await create_test_labware_instance("plate_2")
    await target.place_labware(intruder)

    with pytest.raises(ValueError, match="occupied by plate_2"):
        await executing.execute()
