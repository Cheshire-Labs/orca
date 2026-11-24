"""A stop that lands while a thread carries its labware to the start ends the thread STOPPED.

The carry runs before the method loop, outside the handlers that turn a stop into STOPPED, so a stop
there used to escape as an unhandled error and mark the thread and its execution FAILED. A stop also
withdraws a move request that is still waiting for its reservation.
"""
import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import InMemoryLabwareLocationService
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.system.reservation_manager.errors import MoveAbandonedError
from orca.workflow_models.actions.location_action import LocationAction
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.placements_for_tests import this_tests_placements
from tests.test_helpers import wait_until
from tests.thread_builder import build_executing_thread


def _thread_with_plate_elsewhere() -> tuple[ExecutingLabwareThread, MagicMock]:
    start = Location("pad1", resource=PlatePad("pad1"))
    elsewhere = Location("pad2", resource=PlatePad("pad2"))
    plate = LabwareInstance("plate_96", "_sim_plate")
    service = InMemoryLabwareLocationService(this_tests_placements())
    elsewhere.initialize_labware(plate)
    service.update(plate, elsewhere)
    thread = LabwareThreadInstance(
        labware=plate, start_location=start, end_locations=[start], run_mode=WorkflowRunMode.CHECK_RUN,
    )
    context = MagicMock()
    context.execution_id = "exec-start-move"
    context.workflow_name = "wf-start-move"
    move_handler = MagicMock()
    move_handler.has_route.return_value = True
    status_manager = MagicMock()
    status_manager.get_status.return_value = LabwareThreadStatus.CREATED.name
    executing = build_executing_thread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=move_handler,
        status_manager=status_manager,
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=service,
    )
    return executing, move_handler


async def _waits_until_withdrawn(
    thread_id: str,
    labware: LabwareInstance,
    current_location: Location,
    targets: list[Location],
    assigned_action: LocationAction | None,
    previous_location: Location | None,
    escape_on_arrival: bool,
    abandon_when: Callable[[], bool],
) -> None:
    while not abandon_when():
        await asyncio.sleep(0.01)
    raise MoveAbandonedError("withdrawn")


@pytest.mark.asyncio
async def test_a_stop_while_the_start_move_waits_for_its_reservation_ends_the_thread_stopped() -> None:
    executing, move_handler = _thread_with_plate_elsewhere()
    move_handler.resolve_move_action = AsyncMock(side_effect=_waits_until_withdrawn)

    starting = asyncio.create_task(executing._start_body())
    await wait_until(lambda: move_handler.resolve_move_action.await_count == 1, timeout=5.0)
    executing.stop()

    await asyncio.wait_for(starting, timeout=5.0)
    assert executing._state_machine.current is LabwareThreadStatus.STOPPED
