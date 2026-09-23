"""Typed exceptions for the reservation subsystem.

Lives next to ``reservation_manager.py`` so consumers (the action retry
loop, the move handler) and translators (a hosted error-envelope translator) import
from a stable path without crossing into module bodies.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Protocol, runtime_checkable


@dataclass(frozen=True)
class UnresolvableDeadlockContext:
    """Context for an unresolvable deadlock. Reusable across deadlock variants.

    Round 1 populates this with immovable-blocker findings (the requester's
    reservation rejected by a labware whose owning thread declared
    ``immovable=True``). Future rounds may add more `reason` values for
    other unresolvable patterns without changing the dataclass shape.
    """
    requesting_thread_id: str
    requesting_labware_id: str
    blocking_position_id: str
    blocking_thread_id: str
    blocking_labware_id: str
    reason: str
    hint: str


class UnresolvableDeadlockError(RuntimeError):
    """Engine declared a deadlock with no resolution path.

    Raised from ``ResourcePoolResolver.resolve_action_location`` when the
    detector identified an immovable blocker (or, in future rounds, any
    other unresolvable pattern). Carries an
    ``UnresolvableDeadlockContext`` for operator-facing diagnostics and
    typed translation on the hosted envelope.
    """

    def __init__(self, context: UnresolvableDeadlockContext) -> None:
        self.context = context
        super().__init__(self._format_message(context))

    @staticmethod
    def _format_message(c: UnresolvableDeadlockContext) -> str:
        return (
            f"Unresolvable deadlock: thread '{c.requesting_thread_id}' "
            f"(labware '{c.requesting_labware_id}') waits on location "
            f"'{c.blocking_position_id}' (held by labware "
            f"'{c.blocking_labware_id}', owned by thread "
            f"'{c.blocking_thread_id}'). Reason: {c.reason}. Hint: {c.hint}"
        )


@dataclass(frozen=True)
class ActionFailedContext:
    """Context for an ACTION_FAILED incident.

    Recorded when a default-PAUSE action body raises and the thread parks
    for an operator recovery decision. The thread is already PAUSED with
    its ``last_error`` set; this incident is the queryable mirror so the
    failure surfaces on ``orca incident list`` / ``GET /api/incidents``
    without scraping logs. Defined here (system layer) rather than in
    ``runtime.incident_store`` so the thread (in ``workflow_models``) can
    build it without crossing into ``runtime``; ``incident_store`` reuses
    it as the persisted ``IncidentDetail`` shape, mirroring
    ``UnresolvableDeadlockContext``.

    ``device_command`` names the device call the action was suspended inside
    when it failed, or is None when the surrounding body failed instead. The two
    are different failures with different repairs, and this is what picks the
    ``RecoveryAction`` the incident advises.
    """
    action_command: str
    method_name: str
    error_type: str
    error_message: str
    device_command: str | None


@dataclass(frozen=True)
class ThreadDiedContext:
    """Context for a THREAD_DIED incident.

    Recorded when a thread stops on an error nothing caught, so it never
    reached a recovery pause and there is no paused thread to recover. Its
    labware is wherever the crash left it, and naming that position is the
    point: it is what an operator needs in order to go and clear the deck.
    """
    thread_name: str
    labware_name: str
    labware_id: str
    last_position_id: str | None
    error_type: str
    error_message: str


@dataclass(frozen=True)
class ActionContinuedContext:
    """Context for an ACTION_CONTINUED incident.

    Recorded when an operator answers an errored action with
    ``RecoveryDecision.CONTINUE``: the thread carries on to the next action
    with the failed action's side effects unknown. Nothing re-checks the world
    model against the device afterwards, so this is the durable statement that
    everything downstream of this point is unverified.
    """
    action_command: str
    method_name: str
    error_type: str
    error_message: str


@dataclass(frozen=True)
class MoveFailedContext:
    """Context for a MOVE_FAILED incident.

    Recorded when a default-PAUSE routing move raises and the thread parks
    for an operator recovery decision, the move-side mirror of
    ``ActionFailedContext``. A move has no action/method; it carries the
    source and target sites, the mover, and the labware. All fields are
    plain strings (position_id / name) so ``incident_store`` can persist
    the frozen dataclass directly, same as the other contexts here.
    """
    source: str
    target: str
    transporter: str
    labware: str
    error_type: str
    error_message: str


@dataclass(frozen=True)
class MoveContinuedContext:
    """Context for a MOVE_CONTINUED incident.

    Recorded when an operator answers a failed move with
    ``RecoveryDecision.CONTINUE``: they carried the labware to the target
    themselves and recorded the new position, so the run goes on without the
    arm being sent at the target again. The move-side mirror of
    ``ActionContinuedContext``: what the arm did before it failed is unknown,
    and only the operator's word puts the labware where it now is.
    """
    source: str
    target: str
    transporter: str
    labware: str
    error_type: str
    error_message: str


@dataclass(frozen=True)
class OrphanedBacklogContext:
    """A receiver died ABORTED/STOPPED still owing contributions.

    Built by ``ExecutingWorkflow`` at quarantine time; ``incident_store``
    reuses it as the persisted ``IncidentDetail`` shape (same pattern as
    ``ActionFailedContext``). ``undelivered_count`` covers queue + pending;
    ``in_flight_method_name`` is the dequeued-but-incomplete contribution,
    if any. ``pause_requested_thread_ids`` are requests, not states: the
    named threads pause at their next safe point.
    """
    slot_key: str
    labware_template_name: str
    receiver_thread_id: str
    receiver_thread_name: str
    receiver_status: str
    undelivered_count: int
    in_flight_method_name: str | None
    pause_requested_thread_ids: tuple[str, ...]


class IRecoverableTimeoutCoordinator(Protocol):
    """Engine surface that wraps a device call with a recoverable timeout.

    Defined here (primitive signatures, no incident-store import) so
    ``IThreadIncidentDeclarer`` can expose it without a module cycle. The
    concrete implementation lives in ``orca.runtime.recoverable_timeout``.
    ``run_with_timeout`` returns the device-call result, which is genuinely
    heterogeneous (driver responses), hence ``Any``.
    """

    async def run_with_timeout(
        self,
        execution_id: str,
        device_id: str,
        command: str,
        max_seconds: float,
        coro_factory: Callable[[], Awaitable[Any]],
    ) -> Any: ...

    def extend(self, incident_id: str, additional_seconds: float) -> None: ...

    def abort(self, incident_id: str, operator: str, reason: str) -> None: ...

    def mark_complete(
        self, incident_id: str, operator: str, reason: str,
    ) -> None: ...


@runtime_checkable
class IThreadIncidentDeclarer(Protocol):
    """Contract for converting a thread-originated failure into a recorded
    incident plus the appropriate pause.

    Implemented by ``SystemRuntime``. The Protocol exists so
    ``ExecutingLabwareThread`` and ``ExecutingWorkflow`` (which live in
    ``workflow_models`` and cannot import ``runtime``) can hold a
    back-reference without closing an import cycle.

    ``declare_unresolvable_deadlock`` records the typed
    ``IncidentCategory.UNRESOLVABLE_DEADLOCK`` and fans out
    ``pause_all_threads(execution_id)``.

    ``declare_action_failure`` records the typed
    ``IncidentCategory.ACTION_FAILED`` for a default-PAUSE action error.
    It does NOT fan out a pause -- the raising thread already paused
    itself via ``_pause_for_error``; this only adds the queryable record.
    Its advisory follows the context's ``device_command``: the op-level retry
    when the thread is suspended inside a device call, the whole-action retry
    otherwise.

    ``declare_action_continued`` records the typed
    ``IncidentCategory.ACTION_CONTINUED`` when an operator carries on past an
    errored action, so the unverified stretch of the run is queryable.

    ``declare_unresolved_anchor_insert`` records a WARNING-severity
    ``IncidentCategory.UNRESOLVED_ANCHOR_INSERT`` when a Before/After insert
    is still pending at the end. ``anchor_reached`` separates the two drops: the
    anchor never came past, or it did and the insert did not follow. The drop
    is expected (conditional paths); this only makes it loud. No pause.

    All surface on every operator surface (``orca incident list`` /
    ``GET /api/incidents`` / ``incidents_list`` MCP).
    """

    def declare_unresolvable_deadlock(
        self,
        execution_id: str,
        context: UnresolvableDeadlockContext,
    ) -> object: ...

    def declare_action_failure(
        self,
        execution_id: str,
        thread_id: str,
        context: ActionFailedContext,
    ) -> object: ...

    def declare_thread_death(
        self,
        execution_id: str,
        thread_id: str,
        context: ThreadDiedContext,
    ) -> object: ...

    @property
    def recoverable_timeout_coordinator(self) -> IRecoverableTimeoutCoordinator:
        """The engine's recoverable-timeout coordinator.

        The same single runtime back-ref carries this; an executing thread seeds
        it onto the per-thread ``recoverable_timeout_coordinator`` ContextVar at
        start so device dispatch beneath it can park a timed-out call.
        """
        ...

    def declare_unresolved_anchor_insert(
        self,
        execution_id: str,
        thread_id: str,
        anchor_name: str,
        direction: str,
        target_type: str,
        item_name: str | None,
        anchor_reached: bool,
    ) -> object: ...


    def declare_orphaned_backlog(
        self,
        execution_id: str,
        context: OrphanedBacklogContext,
    ) -> None:
        """Record ``IncidentCategory.ORPHANED_BACKLOG`` for a quarantined
        slot. The workflow already pause-requested the in-scope threads;
        this only adds the queryable record."""
        ...

    def declare_move_failure(
        self,
        execution_id: str,
        thread_id: str,
        context: MoveFailedContext,
    ) -> None:
        """Record ``IncidentCategory.MOVE_FAILED`` for a default-PAUSE routing
        move error. The move-side mirror of ``declare_action_failure``: the
        thread already paused itself, so this only adds the queryable record
        and does not fan out a pause."""
        ...

    def declare_action_continued(
        self,
        execution_id: str,
        thread_id: str,
        context: ActionContinuedContext,
    ) -> None:
        """Record ``IncidentCategory.ACTION_CONTINUED`` when an operator carries
        on past an errored action. WARNING severity: the run is proceeding, but
        with the failed action's side effects unknown."""
        ...

    def declare_move_continued(
        self,
        execution_id: str,
        thread_id: str,
        context: MoveContinuedContext,
    ) -> None:
        """Record ``IncidentCategory.MOVE_CONTINUED`` when an operator finishes
        a failed move by hand and the run carries on from the target. WARNING
        severity, same as the action mirror: nothing failed any more, but the
        labware is where it is on the operator's word alone."""
        ...


class ActionReservationTimeoutError(Exception):
    """Raised when the action-reservation retry loop exceeds its budget.

    The retry loop in ``ResourcePoolResolver.resolve_action_location``
    parks on a reservation collection until it is granted; with
    ``action_reservation_timeout`` set, exceeding the elapsed budget
    raises this instead of looping forever. Carries the thread id,
    the configured timeout, the candidate locations the thread was
    racing against, and whether the last cycle was rejected or
    deadlocked, so the operator-facing envelope (a hosted deployment's
    ``ACTION_RESERVATION_TIMEOUT``) can point at the right deadlock.

    Default ``action_reservation_timeout`` is ``None`` (wait
    indefinitely) on production -- devices regularly hold reservations
    for hours of action execution. Operators set a finite value in
    test harnesses; the error surfaces only in that opt-in mode.
    """

    def __init__(
        self,
        thread_id: str,
        timeout_seconds: float,
        candidate_locations: List[str],
        last_outcome: str,
    ) -> None:
        super().__init__(
            f"Thread {thread_id} timed out after {timeout_seconds}s "
            f"waiting for action reservation on {candidate_locations} "
            f"(last outcome: {last_outcome})"
        )
        self.thread_id = thread_id
        self.timeout_seconds = timeout_seconds
        self.candidate_locations = candidate_locations
        self.last_outcome = last_outcome


class MoveAbandonedError(Exception):
    """Control-flow signal: the caller withdrew a pending move request.

    Raised out of the move-reservation retry loop when the caller's
    ``abandon_when`` predicate turns true on a rejected cycle -- the move
    became moot before it was ever granted (e.g. a parking thread whose
    slot received work mid-wait). All partial holds are already released
    when this raises; the labware simply stays where it rests.
    """


class AcquisitionYieldRequested(Exception):
    """Control-flow signal: an action-acquisition request was deadlock-flagged.

    Under drain-gated release an
    acquisition-blocked thread can itself be the physical blocker: its
    previous device's mutex releases only when its plate leaves, and the
    plate only moves after the NEXT acquisition succeeds. The pre-rule-7
    yield (retry with starvation credit) cannot unwind that swap. The
    thread catches this signal, parks its plate to a resolution pad so
    its pending drain completes, and re-resolves.
    """
