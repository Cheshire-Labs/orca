"""What an operator may do to labware a thread is using, and what the thread is told."""

from unittest.mock import MagicMock

import pytest

from orca.resource_models.device_error import LabwareDischargedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.labware_holds import holds_labware, refusal_to_change_position
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.thread_builder import build_executing_thread


@pytest.mark.parametrize("status", [s for s in LabwareThreadStatus if s.is_terminal()])
def test_a_finished_thread_does_not_hold_its_labware(status: LabwareThreadStatus) -> None:
    assert holds_labware(status) is False


def test_a_thread_parked_for_the_removal_does_not_hold_its_labware() -> None:
    assert holds_labware(LabwareThreadStatus.AWAITING_MANUAL_REMOVE) is False


@pytest.mark.parametrize("status", [
    LabwareThreadStatus.MOVING, LabwareThreadStatus.EXECUTING_ACTION, LabwareThreadStatus.PAUSED, None,
])
def test_a_live_thread_or_an_unreadable_status_holds_the_labware(status: LabwareThreadStatus | None) -> None:
    assert holds_labware(status) is True


@pytest.mark.parametrize("status,text", [
    (LabwareThreadStatus.MOVING, "is being moved"),
    (LabwareThreadStatus.EXECUTING_ACTION, "is in an action"),
])
def test_a_position_cannot_be_changed_while_the_thread_is_using_it(
    status: LabwareThreadStatus, text: str,
) -> None:
    refusal = refusal_to_change_position(status, "plate_1", "journey")
    assert refusal is not None and text in refusal and "journey" in refusal


@pytest.mark.parametrize("status", [
    LabwareThreadStatus.PAUSED,
    LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
    LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY,
    LabwareThreadStatus.AWAITING_MANUAL_PLACE,
])
def test_a_position_can_be_changed_while_the_thread_waits(status: LabwareThreadStatus) -> None:
    assert refusal_to_change_position(status, "plate_1", "journey") is None


def _thread() -> ExecutingLabwareThread:
    target = Location("target", resource=PlatePad("target"))
    thread = LabwareThreadInstance(
        labware=LabwareInstance("plate_96", "96_well"),
        start_location=Location("pad", resource=PlatePad("pad")),
        end_locations=[target],
        run_mode=WorkflowRunMode.CHECK_RUN,
    )
    context = MagicMock()
    context.execution_id = "exec-discharged"
    context.workflow_name = "wf-discharged"
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


def test_a_discharged_thread_treats_every_plan_as_stale() -> None:
    thread = _thread()
    thread.notify_labware_discharged()
    assert thread._plan_is_stale() is True
    assert thread._labware_moved_event.is_set()


class _PausedHere(Exception):
    pass


@pytest.mark.asyncio
async def test_a_discharged_thread_pauses_instead_of_planning_its_next_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread()
    thread.notify_labware_discharged()
    seen: list[Exception] = []

    async def pause(error: Exception) -> None:
        seen.append(error)
        raise _PausedHere()

    monkeypatch.setattr(thread, "_pause_before_replanning", pause)
    monkeypatch.setattr(thread, "_fire", lambda event: None)
    with pytest.raises(_PausedHere):
        await thread._fire_and_execute_move_to(ThreadEvent.MOVE_REPLAN_REQUESTED, [])

    assert isinstance(seen[0], LabwareDischargedError)
    assert "plate_96" in str(seen[0])
    thread._move_handler.resolve_move_action.assert_not_called()
