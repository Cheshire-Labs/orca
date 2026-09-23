"""``MethodStateMachine``: pure state machine for ``MethodStatus``
transitions on ``ExecutingMethod``. Parallel to ``ThreadStateMachine``
(see ``labware_threads/thread_state_machine.py``) but for methods.
"""
from enum import Enum
from typing import Dict, FrozenSet, List, Tuple

from orca.workflow_models.status_enums import MethodStatus


class MethodEvent(str, Enum):
    """Cause-named events that drive ``MethodStatus`` transitions."""

    ACTION_CONSUMED = "ACTION_CONSUMED"
    MARK_SKIPPED = "MARK_SKIPPED"
    ALL_ACTIONS_COMPLETED = "ALL_ACTIONS_COMPLETED"
    METHOD_ABORTED = "METHOD_ABORTED"


_TERMINAL: FrozenSet[MethodStatus] = frozenset({
    MethodStatus.COMPLETED,
    MethodStatus.SKIPPED,
    MethodStatus.PARTIAL_COMPLETE,
})


def _build_transition_table() -> Dict[Tuple[MethodStatus, MethodEvent], MethodStatus]:
    return {
        (MethodStatus.CREATED, MethodEvent.ACTION_CONSUMED): MethodStatus.IN_PROGRESS,
        (MethodStatus.CREATED, MethodEvent.MARK_SKIPPED): MethodStatus.SKIPPED,
        # Generator-backed methods (actions produced via the yield adapter
        # rather than stored on ``method.actions``) start with an empty
        # action lane and complete directly from CREATED.
        (MethodStatus.CREATED, MethodEvent.ALL_ACTIONS_COMPLETED): MethodStatus.COMPLETED,
        (MethodStatus.IN_PROGRESS, MethodEvent.ALL_ACTIONS_COMPLETED): MethodStatus.COMPLETED,
        (MethodStatus.IN_PROGRESS, MethodEvent.METHOD_ABORTED): MethodStatus.PARTIAL_COMPLETE,
    }


METHOD_TRANSITION_TABLE: Dict[Tuple[MethodStatus, MethodEvent], MethodStatus] = _build_transition_table()


class InvalidMethodTransition(Exception):
    """Raised when ``MethodStateMachine.transition`` rejects an event
    for the current state. Carries the current state, the rejected
    event, and the set of legal events for diagnostic logs.
    """

    def __init__(
        self,
        current: MethodStatus,
        event: MethodEvent,
        legal_events: List[MethodEvent],
    ) -> None:
        self.current = current
        self.event = event
        self.legal_events = legal_events
        legal_names = ", ".join(e.name for e in legal_events) if legal_events else "(none)"
        super().__init__(
            f"Cannot transition from {current.name} via {event.name}. "
            f"Legal events from this state: {legal_names}"
        )


class MethodStateMachine:
    """Owns the current ``MethodStatus`` and validates transitions
    against an explicit ``(from_status, event) -> to_status`` table.

    Pure state-machine logic: no I/O, no ``EventBus`` knowledge,
    no terminal-hook side effects. Sibling to ``ThreadStateMachine``.
    """

    def __init__(self, initial: MethodStatus = MethodStatus.CREATED) -> None:
        self._current: MethodStatus = initial

    @property
    def current(self) -> MethodStatus:
        return self._current

    def legal_events(self) -> List[MethodEvent]:
        return [
            event
            for (from_state, event) in METHOD_TRANSITION_TABLE
            if from_state is self._current
        ]

    def transition(self, event: MethodEvent) -> MethodStatus:
        """Apply ``event``; return the new state. Raises
        ``InvalidMethodTransition`` if the table does not permit the
        transition (state is NOT mutated in that case).
        """
        target = METHOD_TRANSITION_TABLE.get((self._current, event))
        if target is None:
            raise InvalidMethodTransition(self._current, event, self.legal_events())
        self._current = target
        return target

    def is_terminal(self) -> bool:
        return self._current in _TERMINAL
