"""A thread waiting for its move target to empty ends that wait when it is stopped.

The target is granted empty, so this wait only happens when a plate lands on it afterwards, which
is usually a person. Before, the wait raced only an operator relocation of this thread's own plate,
so a stop sat behind it until someone cleared the target.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from tests.thread_builder import build_executing_thread


def _thread_waiting_on(target: Location) -> ExecutingLabwareThread:
    source = Location("pad", resource=PlatePad("pad"))
    thread = LabwareThreadInstance(
        labware=LabwareInstance("plate_96", "96_well"),
        start_location=source,
        end_locations=[target],
        run_mode=WorkflowRunMode.CHECK_RUN,
    )
    context = MagicMock()
    context.execution_id = "exec-target-stop"
    context.workflow_name = "wf-target-stop"
    location_service = MagicMock()
    location_service.get_history.return_value = MagicMock()
    executing = build_executing_thread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=location_service,
    )
    move = MagicMock()
    move.target = target
    executing._move_action = move
    return executing


@pytest.mark.asyncio
async def test_a_stop_ends_the_wait_for_a_deadlock_parking_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    executing = _thread_waiting_on(Location("target", resource=PlatePad("target")))
    monkeypatch.setattr(executing, "_plan_is_stale", lambda: False)
    executing._move_handler.handle_deadlock = AsyncMock(return_value=executing._move_action)

    await executing._handle_deadlock()

    abandon_when = executing._move_handler.handle_deadlock.await_args.kwargs["abandon_when"]
    assert abandon_when() is False
    executing._stop_event.set()
    assert abandon_when() is True


@pytest.mark.asyncio
async def test_a_stop_ends_the_wait_for_an_occupied_move_target(monkeypatch: pytest.MonkeyPatch) -> None:
    target = Location("target", resource=PlatePad("target"))
    target.initialize_labware(LabwareInstance("someone_elses_plate", "96_well"))
    executing = _thread_waiting_on(target)
    monkeypatch.setattr(executing, "_plan_is_stale", lambda: False)

    waiting = asyncio.create_task(executing._await_move_target())
    await asyncio.sleep(0)
    assert not waiting.done()

    executing._stop_event.set()
    await asyncio.wait_for(waiting, timeout=5.0)


@pytest.mark.asyncio
async def test_a_wake_that_changes_nothing_keeps_waiting_for_the_occupied_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every operator command wakes waiting threads. A wake whose plan is still good must not start the move."""
    target = Location("target", resource=PlatePad("target"))
    blocker = LabwareInstance("someone_elses_plate", "96_well")
    target.initialize_labware(blocker)
    executing = _thread_waiting_on(target)
    monkeypatch.setattr(executing, "_plan_is_stale", lambda: False)

    waiting = asyncio.create_task(executing._await_move_target())
    await asyncio.sleep(0)
    executing.notify_labware_moved()
    for _ in range(50):
        await asyncio.sleep(0)
    assert not waiting.done()

    await target.dispose_labware(blocker)
    assert await asyncio.wait_for(waiting, timeout=5.0) is True
