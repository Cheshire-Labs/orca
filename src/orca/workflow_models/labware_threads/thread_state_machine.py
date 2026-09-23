from enum import Enum
from typing import Dict, FrozenSet, Set, Tuple

from orca.workflow_models.status_enums import LabwareThreadStatus


class ThreadEvent(str, Enum):
    """Cause-named events that drive ``LabwareThreadStatus`` transitions."""

    CREATED = "CREATED"

    LIVE_MANUAL_PLACE_AWAITED = "LIVE_MANUAL_PLACE_AWAITED"

    RESERVATION_AWAITED = "RESERVATION_AWAITED"
    RESERVATION_GRANTED = "RESERVATION_GRANTED"
    CO_LABWARE_AWAITED = "CO_LABWARE_AWAITED"
    ACTION_RESOLVED = "ACTION_RESOLVED"

    MOVE_RESERVATION_REQUESTED = "MOVE_RESERVATION_REQUESTED"
    MOVE_TO_END_REQUESTED = "MOVE_TO_END_REQUESTED"
    PARK_MOVE_REQUESTED = "PARK_MOVE_REQUESTED"
    PARK_ABANDONED = "PARK_ABANDONED"
    MOVE_REPLAN_REQUESTED = "MOVE_REPLAN_REQUESTED"
    MOVE_TARGET_AWAITED = "MOVE_TARGET_AWAITED"
    MOVE_TARGET_GRANTED = "MOVE_TARGET_GRANTED"
    MOVE_RETRY = "MOVE_RETRY"

    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    ACTION_BODY_RESUMED = "ACTION_BODY_RESUMED"
    ERROR_PAUSE = "ERROR_PAUSE"
    UNRESOLVABLE_DEADLOCK = "UNRESOLVABLE_DEADLOCK"

    RECOVERY_RETRY = "RECOVERY_RETRY"
    RECOVERY_SKIP = "RECOVERY_SKIP"

    STOP_REQUESTED = "STOP_REQUESTED"
    STOP_COMPLETE = "STOP_COMPLETE"
    ABORT_THREAD = "ABORT_THREAD"
    THREAD_FAILED = "THREAD_FAILED"
    THREAD_COMPLETED = "THREAD_COMPLETED"
    LIVE_MANUAL_REMOVE_AWAITED = "LIVE_MANUAL_REMOVE_AWAITED"


_TERMINAL_STATES: FrozenSet[LabwareThreadStatus] = frozenset({
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.FAILED,
})


_NON_TERMINAL: FrozenSet[LabwareThreadStatus] = frozenset(
    s for s in LabwareThreadStatus if s not in _TERMINAL_STATES
)


_NON_TERMINAL_NON_STOPPING: FrozenSet[LabwareThreadStatus] = frozenset(
    s for s in _NON_TERMINAL if s != LabwareThreadStatus.STOPPING
)


def _build_transition_table() -> Dict[Tuple[LabwareThreadStatus, ThreadEvent], LabwareThreadStatus]:
    table: Dict[Tuple[LabwareThreadStatus, ThreadEvent], LabwareThreadStatus] = {}

    # Sorted so table.items() order is stable across processes; frozenset
    # iteration is hash-randomized, which breaks xdist collection of the table test.
    ordered_non_terminal = sorted(_NON_TERMINAL, key=lambda s: s.name)
    ordered_non_terminal_non_stopping = sorted(_NON_TERMINAL_NON_STOPPING, key=lambda s: s.name)

    table[(LabwareThreadStatus.CREATED, ThreadEvent.LIVE_MANUAL_PLACE_AWAITED)] = (
        LabwareThreadStatus.AWAITING_MANUAL_PLACE
    )

    # Reservation manager calls back from any state; status-manager publish
    # is a no-op on same-state, so the wide source set lets the candidate-list
    # payload refresh without depending on caller state.
    for from_state in ordered_non_terminal:
        table[(from_state, ThreadEvent.RESERVATION_AWAITED)] = (
            LabwareThreadStatus.AWAITING_ACTION_RESERVATION
        )
    table[(LabwareThreadStatus.AWAITING_ACTION_RESERVATION, ThreadEvent.RESERVATION_GRANTED)] = (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION
    )

    for from_state in (
        LabwareThreadStatus.CREATED,
        LabwareThreadStatus.AWAITING_MANUAL_PLACE,
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
        LabwareThreadStatus.EXECUTING_ACTION,
        LabwareThreadStatus.MOVING,
        LabwareThreadStatus.AWAITING_CO_THREADS,
    ):
        table[(from_state, ThreadEvent.MOVE_RESERVATION_REQUESTED)] = (
            LabwareThreadStatus.AWAITING_MOVE_RESERVATION
        )

    # A park whose slot received work mid-wait withdraws the move and goes
    # to serve that work: it lands in the parked-receiver wait state.
    table[(LabwareThreadStatus.AWAITING_MOVE_RESERVATION, ThreadEvent.PARK_ABANDONED)] = (
        LabwareThreadStatus.AWAITING_CO_THREADS
    )
    # stop() can land while the park move is still blocked; stop wins, same
    # rule as (STOPPING, CO_LABWARE_AWAITED) below.
    table[(LabwareThreadStatus.STOPPING, ThreadEvent.PARK_ABANDONED)] = (
        LabwareThreadStatus.STOPPING
    )

    # An operator relocated the labware, so the plan this thread holds is
    # stale wherever it had got to: waiting for a grant, waiting for its
    # target, or paused on a failed move. All of those go back to planning.
    for from_state in ordered_non_terminal_non_stopping:
        table[(from_state, ThreadEvent.MOVE_REPLAN_REQUESTED)] = (
            LabwareThreadStatus.AWAITING_MOVE_RESERVATION
        )
    # A stop can land while the plan is being torn up; stop wins, same rule
    # as (STOPPING, PARK_ABANDONED) above.
    table[(LabwareThreadStatus.STOPPING, ThreadEvent.MOVE_REPLAN_REQUESTED)] = (
        LabwareThreadStatus.STOPPING
    )

    table[(LabwareThreadStatus.AWAITING_MOVE_RESERVATION, ThreadEvent.MOVE_TARGET_AWAITED)] = (
        LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY
    )
    table[(LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY, ThreadEvent.MOVE_TARGET_GRANTED)] = (
        LabwareThreadStatus.MOVING
    )

    for from_state in (
        LabwareThreadStatus.CREATED,
        LabwareThreadStatus.AWAITING_MANUAL_PLACE,
        LabwareThreadStatus.MOVING,
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
        LabwareThreadStatus.EXECUTING_ACTION,
        LabwareThreadStatus.AWAITING_CO_THREADS,
    ):
        table[(from_state, ThreadEvent.CO_LABWARE_AWAITED)] = (
            LabwareThreadStatus.AWAITING_CO_THREADS
        )

    # stop() can flip STOPPING between the adapter's stop check and the join-wait
    # fire; stop wins (STOP_COMPLETE's only legal source is STOPPING).
    table[(LabwareThreadStatus.STOPPING, ThreadEvent.CO_LABWARE_AWAITED)] = (
        LabwareThreadStatus.STOPPING
    )

    table[(LabwareThreadStatus.AWAITING_CO_THREADS, ThreadEvent.ACTION_RESOLVED)] = (
        LabwareThreadStatus.EXECUTING_ACTION
    )

    for from_state in ordered_non_terminal:
        table[(from_state, ThreadEvent.PAUSE_REQUESTED)] = LabwareThreadStatus.PAUSED
        table[(from_state, ThreadEvent.ERROR_PAUSE)] = LabwareThreadStatus.PAUSED
        table[(from_state, ThreadEvent.UNRESOLVABLE_DEADLOCK)] = LabwareThreadStatus.PAUSED

    table[(LabwareThreadStatus.PAUSED, ThreadEvent.RESUME_REQUESTED)] = (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION
    )
    # A body held at a manual step never left its device or its action, so it
    # resumes back into the action rather than re-resolving a location.
    table[(LabwareThreadStatus.PAUSED, ThreadEvent.ACTION_BODY_RESUMED)] = (
        LabwareThreadStatus.EXECUTING_ACTION
    )
    table[(LabwareThreadStatus.PAUSED, ThreadEvent.RECOVERY_RETRY)] = (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION
    )
    table[(LabwareThreadStatus.PAUSED, ThreadEvent.RECOVERY_SKIP)] = (
        LabwareThreadStatus.RESOLVING_ACTION_LOCATION
    )
    table[(LabwareThreadStatus.PAUSED, ThreadEvent.MOVE_RETRY)] = LabwareThreadStatus.MOVING

    for from_state in ordered_non_terminal:
        table[(from_state, ThreadEvent.MOVE_TO_END_REQUESTED)] = (
            LabwareThreadStatus.AWAITING_MOVE_RESERVATION
        )
        table[(from_state, ThreadEvent.LIVE_MANUAL_REMOVE_AWAITED)] = (
            LabwareThreadStatus.AWAITING_MANUAL_REMOVE
        )
        table[(from_state, ThreadEvent.PARK_MOVE_REQUESTED)] = (
            LabwareThreadStatus.AWAITING_MOVE_RESERVATION
        )
        table[(from_state, ThreadEvent.THREAD_COMPLETED)] = LabwareThreadStatus.COMPLETED
        table[(from_state, ThreadEvent.ABORT_THREAD)] = LabwareThreadStatus.ABORTED
        # An unhandled error can escape from any live state, including STOPPING
        # and PAUSED (a stop or pause can be in flight when the crash lands).
        table[(from_state, ThreadEvent.THREAD_FAILED)] = LabwareThreadStatus.FAILED

    for from_state in ordered_non_terminal_non_stopping:
        table[(from_state, ThreadEvent.STOP_REQUESTED)] = LabwareThreadStatus.STOPPING
    table[(LabwareThreadStatus.STOPPING, ThreadEvent.STOP_COMPLETE)] = (
        LabwareThreadStatus.STOPPED
    )

    return table


THREAD_TRANSITION_TABLE = _build_transition_table()


class InvalidThreadTransition(RuntimeError):
    """Raised when ``(from_status, event)`` is not in the transition table."""

    def __init__(
        self,
        current: LabwareThreadStatus,
        event: ThreadEvent,
        legal_events: Set[ThreadEvent],
    ) -> None:
        self.current = current
        self.event = event
        self.legal_events = legal_events
        legal_names = sorted(e.name for e in legal_events)
        super().__init__(
            f"Cannot fire {event.name} from {current.name}. "
            f"Legal events: {legal_names}"
        )


class ThreadStateMachine:
    """Pure state-machine: owns current status, validates transitions, no I/O."""

    def __init__(self, initial: LabwareThreadStatus = LabwareThreadStatus.CREATED) -> None:
        self._current = initial

    @property
    def current(self) -> LabwareThreadStatus:
        return self._current

    def transition(self, event: ThreadEvent) -> LabwareThreadStatus:
        key = (self._current, event)
        if key not in THREAD_TRANSITION_TABLE:
            raise InvalidThreadTransition(
                self._current, event, self._legal_events_from(self._current),
            )
        new_state = THREAD_TRANSITION_TABLE[key]
        self._current = new_state
        return new_state

    @staticmethod
    def _legal_events_from(state: LabwareThreadStatus) -> Set[ThreadEvent]:
        return {
            event for (from_state, event) in THREAD_TRANSITION_TABLE
            if from_state == state
        }
