"""A paused thread gives back the device it is not standing on.

A pause has no end date. Holding a device mutex across one locks out every
sibling that needs it, for no work, and the co-labware wait already defers a
pause rather than park on a held device for exactly this reason.

The rule lived at one of the manual-pause sites, written out by hand there, and
at none of the error-pause sites at all. It lives on both pauses now, so a site
cannot forget it. The one exception is a thread resuming into the rest of an
action body it never left.

This file is the unit half. The reproduced defect, at the error pause, is in
``test_a_paused_thread_gives_back_its_device.py``.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.actions.executable_location_action import (
    ExecutableLocationAction,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.labware_threads.reservation_holdover import (
    ReservationHoldover,
)
from orca.workflow_models.status_enums import LabwareThreadStatus


def _thread_holding_a_device(
    status: LabwareThreadStatus = LabwareThreadStatus.PAUSED,
) -> ExecutingLabwareThread:
    """A thread with a device in its holdover, asked to pause."""
    location = Location("pad", resource=PlatePad("pad"))
    thread = LabwareThreadInstance(
        labware=LabwareInstance("plate_96", "96_well"),
        start_location=location,
        end_locations=[location],
        run_mode=WorkflowRunMode.PURE_SIM,
    )
    context = MagicMock()
    context.execution_id = "exec-pause-device"
    context.workflow_name = "wf-pause-device"
    location_service = MagicMock()
    location_service.get_history.return_value = MagicMock()
    status_manager = MagicMock()
    status_manager.get_status.return_value = status.name

    executing = ExecutingLabwareThread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=status_manager,
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=location_service,
    )
    held_action = MagicMock(spec=ExecutableLocationAction)
    held_action.action = MagicMock()
    holdover = ReservationHoldover()
    holdover.acquire_after_action(held_action, owns_reservation=True)
    executing._holdover = holdover
    executing.request_pause()
    return executing


async def _resume_once_parked(executing: ExecutingLabwareThread) -> None:
    """Let the pause park, then let it go. The pause clears the resume event
    before its first await, so it cannot be pre-set and cannot be lost."""
    await asyncio.sleep(0)
    executing._resume_event.set()


@pytest.mark.asyncio
async def test_a_pause_at_a_boundary_releases_the_held_device() -> None:
    executing = _thread_holding_a_device()

    await asyncio.gather(
        executing._handle_manual_pause(), _resume_once_parked(executing)
    )

    assert not executing._holdover.has_current(), (
        "the device is still held while the thread waits on an operator"
    )


@pytest.mark.asyncio
async def test_a_stop_that_beats_the_pause_still_gives_the_device_back() -> None:
    """A stopping thread has even less use for it than a paused one, and this
    is what the site that already released did before the release moved."""
    executing = _thread_holding_a_device()
    executing._stop_event.set()

    await executing._handle_manual_pause()

    assert not executing._holdover.has_current()


@pytest.mark.asyncio
async def test_the_manual_step_hold_keeps_the_device() -> None:
    """The public entry point for a held action body, not just the flag."""
    executing = _thread_holding_a_device(
        status=LabwareThreadStatus.EXECUTING_ACTION
    )

    await asyncio.gather(
        executing.hold_if_pause_requested(), _resume_once_parked(executing)
    )

    assert executing._holdover.has_current()
