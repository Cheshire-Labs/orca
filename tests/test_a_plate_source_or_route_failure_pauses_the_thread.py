"""A stacker that fails and a route that does not exist pause the thread for a person.

Before, both ended the thread FAILED, one of them after the thread had finished all its work.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import orca.workflow_models.spawn_actions as spawn_actions
from orca.devices.device_interfaces import IPlateSource
from orca.gateway.controller.exceptions import CommandExecutionError, CommandNotSavedError, CommandOutcomeUnknownError
from orca.resource_models.device_error import CommandTimeoutAbortedError, PlateDispenseError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.system.reservation_manager.errors import NoRouteError
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from tests.thread_builder import build_executing_thread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.spawn_actions import DispenseSpawn
from orca.workflow_models.status_enums import PauseSite, RecoveryDecision


def _thread() -> ExecutingLabwareThread:
    target = Location("target", resource=PlatePad("target"))
    thread = LabwareThreadInstance(
        labware=LabwareInstance("plate_96", "96_well"),
        start_location=Location("pad", resource=PlatePad("pad")),
        end_locations=[target],
        run_mode=WorkflowRunMode.CHECK_RUN,
    )
    context = MagicMock()
    context.execution_id = "exec-pause"
    context.workflow_name = "wf-pause"
    location_service = MagicMock()
    location_service.get_history.return_value = MagicMock()
    return build_executing_thread(
        thread=thread,
        event_bus=MagicMock(),
        move_handler=MagicMock(),
        status_manager=MagicMock(),
        actions_resolver=MagicMock(),
        context=context,
        labware_location_service=location_service,
    )


class _Paused(Exception):
    pass


async def _first_plan_fails_with(
    error: Exception, monkeypatch: pytest.MonkeyPatch,
) -> list[Exception]:
    thread = _thread()
    thread._move_handler.resolve_move_action = AsyncMock(side_effect=error)
    seen: list[Exception] = []

    async def pause(pause_error: Exception) -> None:
        seen.append(pause_error)
        raise _Paused()

    monkeypatch.setattr(thread, "_pause_before_replanning", pause)
    monkeypatch.setattr(thread, "_fire", lambda event: None)
    with pytest.raises(type(error) if not isinstance(error, NoRouteError) else _Paused):
        await thread._fire_and_execute_move_to(ThreadEvent.MOVE_TO_END_REQUESTED, [])
    return seen


@pytest.mark.asyncio
async def test_a_first_plan_with_no_route_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    error = NoRouteError("No routes found from a to b")
    assert await _first_plan_fails_with(error, monkeypatch) == [error]


@pytest.mark.asyncio
async def test_any_other_first_plan_error_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _first_plan_fails_with(KeyError("nope"), monkeypatch) == []


def _spawn(dispense: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> DispenseSpawn:
    source = MagicMock(spec=IPlateSource)
    source.name = "stacker"
    source.dispense = dispense
    monkeypatch.setattr(spawn_actions, "_resolve_plate_source", lambda location: source)
    return DispenseSpawn(Location("out", resource=PlatePad("out")), MagicMock())


@pytest.mark.asyncio
async def test_a_dispense_that_raises_names_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = _spawn(AsyncMock(side_effect=RuntimeError("stacker is empty")), monkeypatch)
    with pytest.raises(PlateDispenseError, match="stacker could not dispense a plate: stacker is empty"):
        await spawn._dispense()


@pytest.mark.asyncio
async def test_an_operators_abort_of_a_timed_out_dispense_pauses_like_any_failed_dispense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn = _spawn(AsyncMock(side_effect=CommandTimeoutAbortedError("aborted")), monkeypatch)
    with pytest.raises(PlateDispenseError):
        await spawn._dispense()


@pytest.mark.asyncio
async def test_retry_after_a_failed_dispense_acquires_again(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = _thread()
    spawn = MagicMock(spec=DispenseSpawn)
    spawn.acquire = AsyncMock(side_effect=[PlateDispenseError("stacker", RuntimeError("empty")), None])
    pause = AsyncMock(return_value=RecoveryDecision.RETRY)
    monkeypatch.setattr(thread, "_pause_for_error", pause)
    monkeypatch.setattr(thread, "_fire", lambda event: None)

    await thread._acquire_start_labware(spawn)

    assert spawn.acquire.await_count == 2
    assert pause.await_args.args[2] is PauseSite.THREAD_STEP


def _moved_maybe() -> CommandExecutionError:
    error = CommandExecutionError("stacker faulted after it took the command")
    error.may_have_moved = True
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    CommandNotSavedError("'dispense' ran on 'stacker', but saving it to the record failed"),
    CommandOutcomeUnknownError("'dispense' on 'stacker' got no answer.", "inc-1"),
    _moved_maybe(),
], ids=["not_saved", "outcome_unknown", "may_have_moved"])
async def test_a_dispense_that_may_have_happened_is_not_offered_as_a_retry(
    error: Exception, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plate may already be at the output, and a second dispense onto it jams the stacker."""
    spawn = _spawn(AsyncMock(side_effect=error), monkeypatch)
    with pytest.raises(type(error)):
        await spawn._dispense()
