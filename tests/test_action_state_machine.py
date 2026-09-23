"""ActionStateMachine: pure state-machine for ActionStatus transitions.

Parallel to ThreadStateMachine + MethodStateMachine. Owns the
``(from_status, event)`` transition table; raises InvalidActionTransition
on illegal moves. No I/O, no EventBus knowledge.

Test surface:
1. Parametrized transition table: every (from, event, to) row applies cleanly.
2. Illegal-transition raises InvalidActionTransition naming legal events from the current state.
3. DR2 regression: ExecutableLocationAction._execute_action fires real
   ActionStateMachine events instead of the former silent ``self._status``
   writes on a class with no ``_status`` field.
4. F25 centralization: MoveAction events still fire through the shared machine.
"""

from typing import List, Tuple

import pytest

from orca.workflow_models.action_state_machine import (
    ActionEvent,
    ActionStateMachine,
    InvalidActionTransition,
)
from orca.workflow_models.status_enums import ActionStatus


# Independent oracle: hand-written, NOT read from ACTION_TRANSITION_TABLE, so a
# wrong table entry makes the matching row disagree and the test fails.
_EXPECTED_TRANSITIONS: List[
    Tuple[ActionStatus, ActionEvent, ActionStatus]
] = [
    # LocationAction lifecycle.
    (ActionStatus.CREATED, ActionEvent.LABWARE_AWAITED, ActionStatus.AWAITING_CO_THREADS),
    (ActionStatus.AWAITING_CO_THREADS, ActionEvent.ACTION_STARTED, ActionStatus.EXECUTING_ACTION),
    (ActionStatus.EXECUTING_ACTION, ActionEvent.ACTION_COMPLETED, ActionStatus.COMPLETED),
    # MoveAction lifecycle.
    (ActionStatus.CREATED, ActionEvent.MOVE_RESERVATION_AWAITED, ActionStatus.AWAITING_MOVE_RESERVATION),
    (ActionStatus.AWAITING_MOVE_RESERVATION, ActionEvent.MOVE_PREPARING, ActionStatus.PREPARING_TO_MOVE),
    (ActionStatus.PREPARING_TO_MOVE, ActionEvent.MOVE_PICKING, ActionStatus.PICKING),
    (ActionStatus.PICKING, ActionEvent.MOVE_PLACING, ActionStatus.PLACING),
    (ActionStatus.PLACING, ActionEvent.ACTION_COMPLETED, ActionStatus.COMPLETED),
    # already_picked shortcut.
    (ActionStatus.AWAITING_MOVE_RESERVATION, ActionEvent.MOVE_PLACING, ActionStatus.PLACING),
    # Nothing-to-actuate shortcut: the labware is already at the target.
    (ActionStatus.AWAITING_MOVE_RESERVATION, ActionEvent.ACTION_COMPLETED, ActionStatus.COMPLETED),
    # ACTION_ERRORED from a non-terminal status.
    (ActionStatus.EXECUTING_ACTION, ActionEvent.ACTION_ERRORED, ActionStatus.ERRORED),
    # ACTION_CANCELLED from a non-terminal status.
    (ActionStatus.PICKING, ActionEvent.ACTION_CANCELLED, ActionStatus.ABORTED),
]


class TestTransitionTable:
    """Each representative transition produces the literal expected status and
    mutates ``current``."""

    @pytest.mark.parametrize("from_status,event,to_status", _EXPECTED_TRANSITIONS)
    def test_transition_matches_independent_oracle(
        self,
        from_status: ActionStatus,
        event: ActionEvent,
        to_status: ActionStatus,
    ) -> None:
        machine = ActionStateMachine(initial=from_status)
        result = machine.transition(event)
        assert result is to_status
        assert machine.current is to_status


class TestIllegalTransitions:
    """Illegal ``(from, event)`` pairs raise InvalidActionTransition."""

    def test_illegal_from_completed_raises(self) -> None:
        machine = ActionStateMachine(initial=ActionStatus.COMPLETED)
        with pytest.raises(InvalidActionTransition) as exc_info:
            machine.transition(ActionEvent.ACTION_STARTED)
        assert exc_info.value.current == ActionStatus.COMPLETED
        assert exc_info.value.event == ActionEvent.ACTION_STARTED

    def test_illegal_from_errored_raises(self) -> None:
        machine = ActionStateMachine(initial=ActionStatus.ERRORED)
        with pytest.raises(InvalidActionTransition):
            machine.transition(ActionEvent.ACTION_COMPLETED)

    def test_legal_events_listed_in_error(self) -> None:
        machine = ActionStateMachine(initial=ActionStatus.CREATED)
        with pytest.raises(InvalidActionTransition) as exc_info:
            machine.transition(ActionEvent.MOVE_PLACING)
        # CREATED should legally accept LABWARE_AWAITED, MOVE_RESERVATION_AWAITED,
        # and ACTION_ERRORED. None of those is MOVE_PLACING.
        legal_names = {e.name for e in exc_info.value.legal_events}
        assert "LABWARE_AWAITED" in legal_names
        assert "MOVE_RESERVATION_AWAITED" in legal_names
        assert "MOVE_PLACING" not in legal_names


class TestLocationActionLifecycle:
    """End-to-end: CREATED -> AWAITING_CO_THREADS -> EXECUTING_ACTION -> COMPLETED."""

    def test_happy_path(self) -> None:
        m = ActionStateMachine(initial=ActionStatus.CREATED)
        assert m.transition(ActionEvent.LABWARE_AWAITED) == ActionStatus.AWAITING_CO_THREADS
        assert m.transition(ActionEvent.ACTION_STARTED) == ActionStatus.EXECUTING_ACTION
        assert m.transition(ActionEvent.ACTION_COMPLETED) == ActionStatus.COMPLETED

    def test_error_mid_execute(self) -> None:
        m = ActionStateMachine(initial=ActionStatus.CREATED)
        m.transition(ActionEvent.LABWARE_AWAITED)
        m.transition(ActionEvent.ACTION_STARTED)
        assert m.transition(ActionEvent.ACTION_ERRORED) == ActionStatus.ERRORED


class TestMoveActionLifecycle:
    """End-to-end: CREATED -> AWAITING_MOVE_RESERVATION -> PREPARING -> PICKING -> PLACING -> COMPLETED."""

    def test_full_path_not_already_picked(self) -> None:
        m = ActionStateMachine(initial=ActionStatus.CREATED)
        assert m.transition(ActionEvent.MOVE_RESERVATION_AWAITED) == ActionStatus.AWAITING_MOVE_RESERVATION
        assert m.transition(ActionEvent.MOVE_PREPARING) == ActionStatus.PREPARING_TO_MOVE
        assert m.transition(ActionEvent.MOVE_PICKING) == ActionStatus.PICKING
        assert m.transition(ActionEvent.MOVE_PLACING) == ActionStatus.PLACING
        assert m.transition(ActionEvent.ACTION_COMPLETED) == ActionStatus.COMPLETED

    def test_already_picked_shortcut(self) -> None:
        """When the transporter already holds the labware (retry of a
        partially-completed move), the move skips PREPARING_TO_MOVE +
        PICKING and goes straight from AWAITING_MOVE_RESERVATION to PLACING.
        Matches move_action.py::ExecutableMoveAction._execute_action's
        ``already_picked`` branch.
        """
        m = ActionStateMachine(initial=ActionStatus.AWAITING_MOVE_RESERVATION)
        assert m.transition(ActionEvent.MOVE_PLACING) == ActionStatus.PLACING
        assert m.transition(ActionEvent.ACTION_COMPLETED) == ActionStatus.COMPLETED

    def test_error_mid_picking(self) -> None:
        m = ActionStateMachine(initial=ActionStatus.PICKING)
        assert m.transition(ActionEvent.ACTION_ERRORED) == ActionStatus.ERRORED


class TestErroredFromEveryNonTerminal:
    """ACTION_ERRORED is legal from every non-terminal status."""

    @pytest.mark.parametrize(
        "from_status",
        [
            ActionStatus.CREATED,
            ActionStatus.AWAITING_MOVE_RESERVATION,
            ActionStatus.AWAITING_CO_THREADS,
            ActionStatus.EXECUTING_ACTION,
            ActionStatus.PREPARING_TO_MOVE,
            ActionStatus.PICKING,
            ActionStatus.PLACING,
        ],
    )
    def test_action_errored_legal(self, from_status: ActionStatus) -> None:
        m = ActionStateMachine(initial=from_status)
        assert m.transition(ActionEvent.ACTION_ERRORED) == ActionStatus.ERRORED


class TestDR2DeadWriteEliminationOnExecutableLocationAction:
    """DR2 regression: pre-state-machine,
    ``ExecutableLocationAction._execute_action`` wrote
    ``self._status = ActionStatus.AWAITING_CO_THREADS`` and
    ``self._status = ActionStatus.EXECUTING_ACTION`` on a class with no
    ``_status`` field -- silent dead writes that fired no events.

    The wrapper now converts both to ``_fire(ActionEvent.LABWARE_AWAITED)``
    and ``_fire(ActionEvent.ACTION_STARTED)`` calls. This test asserts the
    events now flow through StatusManager.
    """

    @pytest.mark.asyncio
    async def test_labware_awaited_fires_through_status_manager(self) -> None:
        from unittest.mock import MagicMock

        from orca.events.execution_context import MethodExecutionContext
        from orca.workflow_models.actions.executable_location_action import (
            ExecutableLocationAction,
        )

        status_manager = MagicMock()
        action = MagicMock()
        action.id = "act-1"
        action.command = "shake"
        context = MethodExecutionContext(
            execution_id="exec-1",
            workflow_name="wf",
            method_id="m1",
            method_name="method",
            thread_id="t1",
            thread_name="thread1",
            participating_thread_ids=("t1",),
        )

        wrapper = ExecutableLocationAction(
            status_manager=status_manager,
            action=action,
            context=context,
        )

        # Initial publish on construction.
        status_manager.set_status.assert_called_with(
            "ACTION", "act-1", "CREATED",
            status_manager.set_status.call_args.args[3],
        )

        wrapper._fire(ActionEvent.LABWARE_AWAITED)
        wrapper._fire(ActionEvent.ACTION_STARTED)

        # Pins the dead-write elimination: both transitions reach StatusManager
        # rather than getting absorbed by an in-memory `_status =` shadow.
        statuses_published = [
            c.args[2] for c in status_manager.set_status.call_args_list
        ]
        assert "AWAITING_CO_THREADS" in statuses_published
        assert "EXECUTING_ACTION" in statuses_published
        assert wrapper.status == ActionStatus.EXECUTING_ACTION


class TestF25ExecutableMoveActionEventsThroughSharedMachine:
    """F25 centralization: ``ExecutableMoveAction`` events were never dead;
    they fired correctly via the property setter. They still fire today,
    now routed through the shared ActionStateMachine. This test pins that
    the centralization preserved the event sequence.
    """

    @pytest.mark.asyncio
    async def test_move_lifecycle_events_still_publish(self) -> None:
        from unittest.mock import MagicMock

        from orca.events.execution_context import ThreadExecutionContext
        from tests.test_helpers import make_labware_placer, no_source_hold
        from orca.workflow_models.actions.move_action import ExecutableMoveAction

        status_manager = MagicMock()
        action = MagicMock()
        action.id = "move-1"
        context = ThreadExecutionContext(
            execution_id="exec-1",
            workflow_name="wf",
            thread_id="t1",
            thread_name="thread1",
            template_name="tmpl",
        )
        lls = MagicMock()

        wrapper = ExecutableMoveAction(
            status_manager=status_manager,
            context=context,
            action=action,
            labware_location_service=lls,
            labware_placer=make_labware_placer(lls),
        slot_holder=no_source_hold(),
        )

        # Construction publishes CREATED then transitions to
        # AWAITING_MOVE_RESERVATION (mirrors the wrapper's two-call init in
        # ExecutableMoveAction.__init__).
        published = [c.args[2] for c in status_manager.set_status.call_args_list]
        assert published[0] == "CREATED"
        assert published[1] == "AWAITING_MOVE_RESERVATION"
        assert wrapper.status == ActionStatus.AWAITING_MOVE_RESERVATION

        # Drive the move lifecycle through _fire calls (mirroring
        # _execute_action's transitions).
        wrapper._fire(ActionEvent.MOVE_PREPARING)
        wrapper._fire(ActionEvent.MOVE_PICKING)
        wrapper._fire(ActionEvent.MOVE_PLACING)
        wrapper._fire(ActionEvent.ACTION_COMPLETED)

        published = [c.args[2] for c in status_manager.set_status.call_args_list]
        assert published == [
            "CREATED", "AWAITING_MOVE_RESERVATION", "PREPARING_TO_MOVE",
            "PICKING", "PLACING", "COMPLETED",
        ]
