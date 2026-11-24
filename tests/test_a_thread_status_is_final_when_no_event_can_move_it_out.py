"""A thread status is final when no thread event can move the thread out of it."""

import pytest

from orca.workflow_models.labware_threads.thread_state_machine import (
    InvalidThreadTransition,
    ThreadEvent,
    ThreadStateMachine,
)
from orca.workflow_models.status_enums import LabwareThreadStatus

FINAL_STATUSES = frozenset({
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.FAILED,
})


def _events_that_move_a_thread_out_of(status: LabwareThreadStatus) -> list[ThreadEvent]:
    moving: list[ThreadEvent] = []
    for event in ThreadEvent:
        try:
            ThreadStateMachine(initial=status).transition(event)
        except InvalidThreadTransition:
            continue
        moving.append(event)
    return moving


@pytest.mark.parametrize("status", list(LabwareThreadStatus), ids=lambda s: s.name)
def test_only_completed_aborted_stopped_and_failed_threads_accept_no_event(
    status: LabwareThreadStatus,
) -> None:
    accepts_no_event = not _events_that_move_a_thread_out_of(status)

    assert accepts_no_event == (status in FINAL_STATUSES)


@pytest.mark.parametrize(
    ("event", "ends_in"),
    [
        (ThreadEvent.STOP_COMPLETE, LabwareThreadStatus.STOPPED),
        (ThreadEvent.ABORT_THREAD, LabwareThreadStatus.ABORTED),
        (ThreadEvent.THREAD_FAILED, LabwareThreadStatus.FAILED),
        (ThreadEvent.THREAD_COMPLETED, LabwareThreadStatus.COMPLETED),
        (ThreadEvent.PAUSE_REQUESTED, LabwareThreadStatus.PAUSED),
    ],
    ids=lambda value: value.name,
)
def test_a_stopping_thread_is_not_final_and_an_event_still_moves_it(
    event: ThreadEvent, ends_in: LabwareThreadStatus,
) -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.STOPPING)

    assert machine.transition(event) == ends_in
