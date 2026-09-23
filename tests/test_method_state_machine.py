"""Unit tests for MethodStateMachine."""
from typing import List, Tuple

import pytest

from orca.workflow_models.method_state_machine import (
    InvalidMethodTransition,
    MethodEvent,
    MethodStateMachine,
)
from orca.workflow_models.status_enums import MethodStatus


# Independent oracle: hand-written, NOT read from METHOD_TRANSITION_TABLE, so a
# wrong table entry makes the matching row disagree and the test fails.
_EXPECTED_TRANSITIONS: List[Tuple[MethodStatus, MethodEvent, MethodStatus]] = [
    (MethodStatus.CREATED, MethodEvent.ACTION_CONSUMED, MethodStatus.IN_PROGRESS),
    (MethodStatus.CREATED, MethodEvent.MARK_SKIPPED, MethodStatus.SKIPPED),
    (MethodStatus.CREATED, MethodEvent.ALL_ACTIONS_COMPLETED, MethodStatus.COMPLETED),
    (MethodStatus.IN_PROGRESS, MethodEvent.ALL_ACTIONS_COMPLETED, MethodStatus.COMPLETED),
    (MethodStatus.IN_PROGRESS, MethodEvent.METHOD_ABORTED, MethodStatus.PARTIAL_COMPLETE),
]


@pytest.mark.parametrize("from_state,event,to_state", _EXPECTED_TRANSITIONS)
def test_transition_matches_independent_oracle(
    from_state: MethodStatus,
    event: MethodEvent,
    to_state: MethodStatus,
) -> None:
    machine = MethodStateMachine(initial=from_state)
    result = machine.transition(event)
    assert result is to_state
    assert machine.current is to_state


def test_initial_state_is_created_by_default() -> None:
    machine = MethodStateMachine()
    assert machine.current is MethodStatus.CREATED


def test_initial_state_override() -> None:
    machine = MethodStateMachine(initial=MethodStatus.IN_PROGRESS)
    assert machine.current is MethodStatus.IN_PROGRESS


def test_illegal_transition_raises_with_diagnostic() -> None:
    machine = MethodStateMachine(initial=MethodStatus.COMPLETED)
    with pytest.raises(InvalidMethodTransition) as exc_info:
        machine.transition(MethodEvent.ACTION_CONSUMED)
    err = exc_info.value
    assert err.current is MethodStatus.COMPLETED
    assert err.event is MethodEvent.ACTION_CONSUMED
    assert err.legal_events == []


def test_illegal_transition_does_not_mutate_state() -> None:
    machine = MethodStateMachine(initial=MethodStatus.IN_PROGRESS)
    with pytest.raises(InvalidMethodTransition):
        machine.transition(MethodEvent.MARK_SKIPPED)
    assert machine.current is MethodStatus.IN_PROGRESS


def test_terminal_states_reject_all_events() -> None:
    for terminal in (
        MethodStatus.COMPLETED,
        MethodStatus.SKIPPED,
        MethodStatus.PARTIAL_COMPLETE,
    ):
        machine = MethodStateMachine(initial=terminal)
        for event in MethodEvent:
            with pytest.raises(InvalidMethodTransition):
                machine.transition(event)


def test_normal_completion_path() -> None:
    machine = MethodStateMachine()
    machine.transition(MethodEvent.ACTION_CONSUMED)
    assert machine.current is MethodStatus.IN_PROGRESS
    machine.transition(MethodEvent.ALL_ACTIONS_COMPLETED)
    assert machine.current is MethodStatus.COMPLETED


def test_skip_path() -> None:
    machine = MethodStateMachine()
    machine.transition(MethodEvent.MARK_SKIPPED)
    assert machine.current is MethodStatus.SKIPPED


def test_abort_path() -> None:
    machine = MethodStateMachine()
    machine.transition(MethodEvent.ACTION_CONSUMED)
    machine.transition(MethodEvent.METHOD_ABORTED)
    assert machine.current is MethodStatus.PARTIAL_COMPLETE


def test_is_terminal() -> None:
    machine = MethodStateMachine()
    assert not machine.is_terminal()
    machine.transition(MethodEvent.MARK_SKIPPED)
    assert machine.is_terminal()


def test_legal_events_from_created() -> None:
    machine = MethodStateMachine(initial=MethodStatus.CREATED)
    legal = set(machine.legal_events())
    assert legal == {
        MethodEvent.ACTION_CONSUMED,
        MethodEvent.MARK_SKIPPED,
        MethodEvent.ALL_ACTIONS_COMPLETED,
    }


def test_legal_events_from_in_progress() -> None:
    machine = MethodStateMachine(initial=MethodStatus.IN_PROGRESS)
    legal = set(machine.legal_events())
    assert legal == {MethodEvent.ALL_ACTIONS_COMPLETED, MethodEvent.METHOD_ABORTED}


def test_legal_events_from_terminal_is_empty() -> None:
    machine = MethodStateMachine(initial=MethodStatus.COMPLETED)
    assert machine.legal_events() == []
