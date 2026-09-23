"""ActionStateMachine: pure state-machine for ActionStatus transitions.

Parallel to ``ThreadStateMachine`` and ``MethodStateMachine``. Owns the
``(from_status, event) -> to_status`` transition table; raises
``InvalidActionTransition`` on illegal moves. No I/O, no EventBus
knowledge.

The wrappers (``ExecutableLocationAction`` / ``ExecutableMoveAction``)
own a ``_fire(event)`` orchestrator that calls ``transition(event)``
then publishes through ``StatusManager``. The wrappers are
execution-context-bound lifecycle stages; the bare
``LocationAction`` / ``MoveAction`` carry identity + data without
the execution dependencies.

Asymmetry vs ``ThreadStateMachine``: ASM has only
``_NON_TERMINAL_ACTIVE``; TSM has both ``_NON_TERMINAL`` AND
``_NON_TERMINAL_NON_STOPPING`` because threads carry a STOPPING
intermediate state. Actions have no STOPPING equivalent -- the
lifecycle is narrower and the asymmetry is deliberate, not an
oversight.

Empirical scope: the table enumerates only transitions that have real
writers today. ``RESOLVED``, ``AWAITING_LOCATION_RESERVATION`` and
``SKIPPED`` exist in :class:`ActionStatus` but have zero writers anywhere
in the codebase today. They are not in the table; if a future caller
writes them, the table gains rows. ``ABORTED`` is the terminal target of
``ACTION_CANCELLED``, fired when ``stop_execution`` cancels the owner
task mid-action.
"""

from enum import Enum
from typing import Dict, FrozenSet, Set, Tuple

from orca.workflow_models.status_enums import ActionStatus


class ActionEvent(str, Enum):
    """Cause-named events that drive ``ActionStatus`` transitions."""

    LABWARE_AWAITED = "LABWARE_AWAITED"
    ACTION_STARTED = "ACTION_STARTED"

    MOVE_RESERVATION_AWAITED = "MOVE_RESERVATION_AWAITED"
    MOVE_PREPARING = "MOVE_PREPARING"
    MOVE_PICKING = "MOVE_PICKING"
    MOVE_PLACING = "MOVE_PLACING"

    ACTION_COMPLETED = "ACTION_COMPLETED"
    ACTION_ERRORED = "ACTION_ERRORED"
    ACTION_CANCELLED = "ACTION_CANCELLED"


_NON_TERMINAL_ACTIVE: FrozenSet[ActionStatus] = frozenset({
    ActionStatus.CREATED,
    ActionStatus.AWAITING_MOVE_RESERVATION,
    ActionStatus.AWAITING_CO_THREADS,
    ActionStatus.EXECUTING_ACTION,
    ActionStatus.PREPARING_TO_MOVE,
    ActionStatus.PICKING,
    ActionStatus.PLACING,
})


def _build_transition_table() -> Dict[Tuple[ActionStatus, ActionEvent], ActionStatus]:
    table: Dict[Tuple[ActionStatus, ActionEvent], ActionStatus] = {}

    # LocationAction lifecycle: CREATED -> AWAITING_CO_THREADS -> EXECUTING_ACTION -> COMPLETED.
    # ExecutableLocationAction._execute_action drives these transitions through
    # ``_fire(event)`` against the rows below.
    table[(ActionStatus.CREATED, ActionEvent.LABWARE_AWAITED)] = ActionStatus.AWAITING_CO_THREADS
    table[(ActionStatus.AWAITING_CO_THREADS, ActionEvent.ACTION_STARTED)] = ActionStatus.EXECUTING_ACTION
    table[(ActionStatus.EXECUTING_ACTION, ActionEvent.ACTION_COMPLETED)] = ActionStatus.COMPLETED

    # MoveAction lifecycle: CREATED -> AWAITING_MOVE_RESERVATION -> ... -> PLACING -> COMPLETED.
    table[(ActionStatus.CREATED, ActionEvent.MOVE_RESERVATION_AWAITED)] = ActionStatus.AWAITING_MOVE_RESERVATION
    table[(ActionStatus.AWAITING_MOVE_RESERVATION, ActionEvent.MOVE_PREPARING)] = ActionStatus.PREPARING_TO_MOVE
    table[(ActionStatus.PREPARING_TO_MOVE, ActionEvent.MOVE_PICKING)] = ActionStatus.PICKING
    table[(ActionStatus.PICKING, ActionEvent.MOVE_PLACING)] = ActionStatus.PLACING
    table[(ActionStatus.PLACING, ActionEvent.ACTION_COMPLETED)] = ActionStatus.COMPLETED

    # already_picked shortcut: the transporter already holds the labware from a
    # prior partially-completed attempt; the move skips PREPARING_TO_MOVE + PICKING.
    # move_action.py::ExecutableMoveAction._execute_action gates the PREPARING/PICKING
    # block on ``not already_picked``.
    table[(ActionStatus.AWAITING_MOVE_RESERVATION, ActionEvent.MOVE_PLACING)] = ActionStatus.PLACING

    # Nothing-to-actuate shortcut: the labware is already at the target (an
    # operator carried it there), so the move completes without pick or place.
    table[(ActionStatus.AWAITING_MOVE_RESERVATION, ActionEvent.ACTION_COMPLETED)] = ActionStatus.COMPLETED

    # Sorted so table.items() order is stable across processes; frozenset
    # iteration is hash-randomized, which breaks xdist collection of the table test.
    ordered_active = sorted(_NON_TERMINAL_ACTIVE, key=lambda s: s.name)

    # ACTION_ERRORED is legal from every non-terminal status (an execute() try/except
    # path lands here regardless of where the failure was raised).
    for from_state in ordered_active:
        table[(from_state, ActionEvent.ACTION_ERRORED)] = ActionStatus.ERRORED

    # ACTION_CANCELLED is legal from every non-terminal status: stop_execution
    # cancels the owner task, and CancelledError can interrupt the action at any
    # lifecycle stage. The terminal target is ABORTED, distinct from ERRORED so
    # an operator-initiated stop is not conflated with an action-body failure.
    for from_state in ordered_active:
        table[(from_state, ActionEvent.ACTION_CANCELLED)] = ActionStatus.ABORTED

    return table


ACTION_TRANSITION_TABLE = _build_transition_table()


class InvalidActionTransition(RuntimeError):
    """Raised when ``(from_status, event)`` is not in the transition table."""

    def __init__(
        self,
        current: ActionStatus,
        event: ActionEvent,
        legal_events: Set[ActionEvent],
    ) -> None:
        self.current = current
        self.event = event
        self.legal_events = legal_events
        legal_names = sorted(e.name for e in legal_events)
        super().__init__(
            f"Cannot fire {event.name} from {current.name}. "
            f"Legal events: {legal_names}"
        )


class ActionStateMachine:
    """Pure state-machine: owns current status, validates transitions, no I/O."""

    def __init__(self, initial: ActionStatus = ActionStatus.CREATED) -> None:
        self._current = initial

    @property
    def current(self) -> ActionStatus:
        return self._current

    def transition(self, event: ActionEvent) -> ActionStatus:
        key = (self._current, event)
        if key not in ACTION_TRANSITION_TABLE:
            raise InvalidActionTransition(
                self._current, event, self._legal_events_from(self._current),
            )
        new_state = ACTION_TRANSITION_TABLE[key]
        self._current = new_state
        return new_state

    @staticmethod
    def _legal_events_from(state: ActionStatus) -> Set[ActionEvent]:
        return {
            event for (from_state, event) in ACTION_TRANSITION_TABLE
            if from_state == state
        }
