"""Unit tests for ThreadStateMachine."""
from typing import List, Tuple

import pytest

from orca.workflow_models.labware_threads.thread_state_machine import (
    InvalidThreadTransition,
    ThreadEvent,
    ThreadStateMachine,
)
from orca.workflow_models.status_enums import LabwareThreadStatus


# Independent oracle: hand-written, NOT read from THREAD_TRANSITION_TABLE, so a
# wrong table entry makes the matching row disagree and the test fails.
_EXPECTED_TRANSITIONS: List[
    Tuple[LabwareThreadStatus, ThreadEvent, LabwareThreadStatus]
] = [
    (
        LabwareThreadStatus.CREATED,
        ThreadEvent.LIVE_MANUAL_PLACE_AWAITED,
        LabwareThreadStatus.AWAITING_MANUAL_PLACE,
    ),
    (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
        ThreadEvent.RESERVATION_AWAITED,
        LabwareThreadStatus.AWAITING_ACTION_RESERVATION,
    ),
    (
        LabwareThreadStatus.AWAITING_ACTION_RESERVATION,
        ThreadEvent.RESERVATION_GRANTED,
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
    ),
    (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
        ThreadEvent.MOVE_RESERVATION_REQUESTED,
        LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
    ),
    (
        LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
        ThreadEvent.MOVE_TARGET_AWAITED,
        LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY,
    ),
    (
        LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY,
        ThreadEvent.MOVE_TARGET_GRANTED,
        LabwareThreadStatus.MOVING,
    ),
    (
        LabwareThreadStatus.MOVING,
        ThreadEvent.CO_LABWARE_AWAITED,
        LabwareThreadStatus.AWAITING_CO_THREADS,
    ),
    (
        LabwareThreadStatus.AWAITING_CO_THREADS,
        ThreadEvent.ACTION_RESOLVED,
        LabwareThreadStatus.EXECUTING_ACTION,
    ),
    (
        LabwareThreadStatus.EXECUTING_ACTION,
        ThreadEvent.PAUSE_REQUESTED,
        LabwareThreadStatus.PAUSED,
    ),
    (
        LabwareThreadStatus.EXECUTING_ACTION,
        ThreadEvent.ERROR_PAUSE,
        LabwareThreadStatus.PAUSED,
    ),
    (
        LabwareThreadStatus.EXECUTING_ACTION,
        ThreadEvent.UNRESOLVABLE_DEADLOCK,
        LabwareThreadStatus.PAUSED,
    ),
    (
        LabwareThreadStatus.PAUSED,
        ThreadEvent.RESUME_REQUESTED,
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
    ),
    (
        LabwareThreadStatus.PAUSED,
        ThreadEvent.ACTION_BODY_RESUMED,
        LabwareThreadStatus.EXECUTING_ACTION,
    ),
    (
        LabwareThreadStatus.PAUSED,
        ThreadEvent.RECOVERY_RETRY,
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
    ),
    (
        LabwareThreadStatus.PAUSED,
        ThreadEvent.RECOVERY_SKIP,
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
    ),
    (
        LabwareThreadStatus.PAUSED,
        ThreadEvent.MOVE_RETRY,
        LabwareThreadStatus.MOVING,
    ),
    (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
        ThreadEvent.MOVE_TO_END_REQUESTED,
        LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
    ),
    (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
        ThreadEvent.PARK_MOVE_REQUESTED,
        LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
    ),
    (
        LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
        ThreadEvent.PARK_ABANDONED,
        LabwareThreadStatus.AWAITING_CO_THREADS,
    ),
    (
        LabwareThreadStatus.STOPPING,
        ThreadEvent.PARK_ABANDONED,
        LabwareThreadStatus.STOPPING,
    ),
    (
        LabwareThreadStatus.EXECUTING_ACTION,
        ThreadEvent.LIVE_MANUAL_REMOVE_AWAITED,
        LabwareThreadStatus.AWAITING_MANUAL_REMOVE,
    ),
    (
        LabwareThreadStatus.EXECUTING_ACTION,
        ThreadEvent.THREAD_COMPLETED,
        LabwareThreadStatus.COMPLETED,
    ),
    (
        LabwareThreadStatus.EXECUTING_ACTION,
        ThreadEvent.ABORT_THREAD,
        LabwareThreadStatus.ABORTED,
    ),
    (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
        ThreadEvent.STOP_REQUESTED,
        LabwareThreadStatus.STOPPING,
    ),
    (
        LabwareThreadStatus.STOPPING,
        ThreadEvent.STOP_COMPLETE,
        LabwareThreadStatus.STOPPED,
    ),
    (
        LabwareThreadStatus.AWAITING_CO_THREADS,
        ThreadEvent.MOVE_RESERVATION_REQUESTED,
        LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
    ),
    (
        LabwareThreadStatus.STOPPING,
        ThreadEvent.CO_LABWARE_AWAITED,
        LabwareThreadStatus.STOPPING,
    ),
]


@pytest.mark.parametrize("from_state,event,to_state", _EXPECTED_TRANSITIONS)
def test_transition_matches_independent_oracle(
    from_state: LabwareThreadStatus,
    event: ThreadEvent,
    to_state: LabwareThreadStatus,
) -> None:
    machine = ThreadStateMachine(initial=from_state)
    result = machine.transition(event)
    assert result is to_state
    assert machine.current is to_state


def test_initial_state_is_created_by_default() -> None:
    machine = ThreadStateMachine()
    assert machine.current is LabwareThreadStatus.CREATED


def test_initial_state_override() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.EXECUTING_ACTION)
    assert machine.current is LabwareThreadStatus.EXECUTING_ACTION


def test_illegal_transition_raises_with_diagnostic() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.CREATED)
    with pytest.raises(InvalidThreadTransition) as exc_info:
        machine.transition(ThreadEvent.MOVE_TARGET_GRANTED)
    err = exc_info.value
    assert err.current is LabwareThreadStatus.CREATED
    assert err.event is ThreadEvent.MOVE_TARGET_GRANTED
    assert ThreadEvent.LIVE_MANUAL_PLACE_AWAITED in err.legal_events
    assert ThreadEvent.MOVE_TARGET_GRANTED not in err.legal_events


def test_illegal_transition_does_not_mutate_state() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.RESOLVING_ACTION_LOCATION)
    with pytest.raises(InvalidThreadTransition):
        machine.transition(ThreadEvent.STOP_COMPLETE)
    assert machine.current is LabwareThreadStatus.RESOLVING_ACTION_LOCATION


def test_terminal_states_reject_all_events() -> None:
    for terminal in (
        LabwareThreadStatus.COMPLETED,
        LabwareThreadStatus.ABORTED,
        LabwareThreadStatus.STOPPED,
    ):
        machine = ThreadStateMachine(initial=terminal)
        for event in ThreadEvent:
            with pytest.raises(InvalidThreadTransition):
                machine.transition(event)


def test_pause_requested_legal_from_all_non_terminal_states() -> None:
    non_terminal = [
        s for s in LabwareThreadStatus
        if s not in {
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
        }
    ]
    for s in non_terminal:
        machine = ThreadStateMachine(initial=s)
        result = machine.transition(ThreadEvent.PAUSE_REQUESTED)
        assert result is LabwareThreadStatus.PAUSED


def test_abort_thread_legal_from_all_non_terminal_states() -> None:
    non_terminal = [
        s for s in LabwareThreadStatus
        if s not in {
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
        }
    ]
    for s in non_terminal:
        machine = ThreadStateMachine(initial=s)
        result = machine.transition(ThreadEvent.ABORT_THREAD)
        assert result is LabwareThreadStatus.ABORTED


def test_stop_requested_legal_from_all_non_terminal_non_stopping_states() -> None:
    valid_sources = [
        s for s in LabwareThreadStatus
        if s not in {
            LabwareThreadStatus.COMPLETED,
            LabwareThreadStatus.ABORTED,
            LabwareThreadStatus.STOPPED,
            LabwareThreadStatus.FAILED,
            LabwareThreadStatus.STOPPING,
        }
    ]
    for s in valid_sources:
        machine = ThreadStateMachine(initial=s)
        result = machine.transition(ThreadEvent.STOP_REQUESTED)
        assert result is LabwareThreadStatus.STOPPING


def test_stop_complete_only_from_stopping() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.STOPPING)
    result = machine.transition(ThreadEvent.STOP_COMPLETE)
    assert result is LabwareThreadStatus.STOPPED

    for s in LabwareThreadStatus:
        if s is LabwareThreadStatus.STOPPING:
            continue
        machine = ThreadStateMachine(initial=s)
        with pytest.raises(InvalidThreadTransition):
            machine.transition(ThreadEvent.STOP_COMPLETE)


def test_reservation_roundtrip_is_paired() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.RESOLVING_ACTION_LOCATION)
    machine.transition(ThreadEvent.RESERVATION_AWAITED)
    assert machine.current is LabwareThreadStatus.AWAITING_ACTION_RESERVATION
    machine.transition(ThreadEvent.RESERVATION_GRANTED)
    assert machine.current is LabwareThreadStatus.RESOLVING_ACTION_LOCATION


def test_move_sequence_advances_through_states() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.RESOLVING_ACTION_LOCATION)
    machine.transition(ThreadEvent.MOVE_RESERVATION_REQUESTED)
    assert machine.current is LabwareThreadStatus.AWAITING_MOVE_RESERVATION
    machine.transition(ThreadEvent.MOVE_TARGET_AWAITED)
    assert machine.current is LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY
    machine.transition(ThreadEvent.MOVE_TARGET_GRANTED)
    assert machine.current is LabwareThreadStatus.MOVING


def test_co_labware_then_action_resolves() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.MOVING)
    machine.transition(ThreadEvent.CO_LABWARE_AWAITED)
    assert machine.current is LabwareThreadStatus.AWAITING_CO_THREADS
    machine.transition(ThreadEvent.ACTION_RESOLVED)
    assert machine.current is LabwareThreadStatus.EXECUTING_ACTION


def test_join_dequeue_then_move_is_legal() -> None:
    """A receiver whose dequeued join method resolves at another location
    moves directly out of AWAITING_CO_THREADS."""
    machine = ThreadStateMachine(initial=LabwareThreadStatus.AWAITING_CO_THREADS)
    result = machine.transition(ThreadEvent.MOVE_RESERVATION_REQUESTED)
    assert result is LabwareThreadStatus.AWAITING_MOVE_RESERVATION


def test_co_labware_awaited_during_stop_keeps_stopping() -> None:
    """stop() can flip STOPPING between the adapter's stop check and the
    join-wait CO_LABWARE_AWAITED fire. The event must be legal there but
    must NOT exit STOPPING: STOP_COMPLETE's only legal source is STOPPING."""
    machine = ThreadStateMachine(initial=LabwareThreadStatus.STOPPING)
    assert machine.transition(ThreadEvent.CO_LABWARE_AWAITED) is LabwareThreadStatus.STOPPING
    assert machine.transition(ThreadEvent.STOP_COMPLETE) is LabwareThreadStatus.STOPPED


def test_pause_recovery_retry_returns_to_resolving() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.EXECUTING_ACTION)
    machine.transition(ThreadEvent.ERROR_PAUSE)
    assert machine.current is LabwareThreadStatus.PAUSED
    machine.transition(ThreadEvent.RECOVERY_RETRY)
    assert machine.current is LabwareThreadStatus.RESOLVING_ACTION_LOCATION


def test_pause_move_retry_returns_to_moving() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.MOVING)
    machine.transition(ThreadEvent.ERROR_PAUSE)
    assert machine.current is LabwareThreadStatus.PAUSED
    machine.transition(ThreadEvent.MOVE_RETRY)
    assert machine.current is LabwareThreadStatus.MOVING


def test_unresolvable_deadlock_targets_paused() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.EXECUTING_ACTION)
    machine.transition(ThreadEvent.UNRESOLVABLE_DEADLOCK)
    assert machine.current is LabwareThreadStatus.PAUSED


def test_invalid_transition_message_lists_legal_events() -> None:
    machine = ThreadStateMachine(initial=LabwareThreadStatus.AWAITING_ACTION_RESERVATION)
    with pytest.raises(InvalidThreadTransition) as exc_info:
        machine.transition(ThreadEvent.MOVE_TARGET_GRANTED)
    msg = str(exc_info.value)
    assert "AWAITING_ACTION_RESERVATION" in msg
    assert "MOVE_TARGET_GRANTED" in msg
    assert "RESERVATION_GRANTED" in msg
