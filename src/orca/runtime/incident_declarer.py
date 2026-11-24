from collections.abc import Callable
from typing import Protocol

from orca.runtime.incident_service import IIncidentService
from orca.runtime.incident_store import (
    DeviceInitFailedDetail,
    IncidentCategory,
    IncidentSeverity,
    RecoveryAction,
    SystemIncident,
    UnresolvedAnchorInsertDetail,
)
from orca.runtime.recoverable_timeout import RecoverableTimeoutCoordinator
from orca.modes.sim_pacing import SimPacer
from orca.system.errors import DeviceInitializationError
from orca.system.reservation_manager.errors import (
    ActionContinuedContext,
    ActionFailedContext,
    AutoSpawnFailedDetail,
    CoLabwareTimeoutDetail,
    MoveContinuedContext,
    MoveFailedContext,
    OrphanedBacklogContext,
    ThreadDiedContext,
    UnresolvableDeadlockContext,
)


class PauseExecution(Protocol):
    def __call__(
        self, execution_id: str, reason: str = "manual", message: str | None = None,
    ) -> dict[str, int]: ...


class IncidentDeclarer:
    """Records incidents for the engine; the IThreadIncidentDeclarer the threads and workflows hold."""

    def __init__(
        self,
        incident_service: IIncidentService,
        pause_execution: PauseExecution,
        recoverable_timeouts: RecoverableTimeoutCoordinator,
        sim_pacer_for: Callable[[str], SimPacer | None],
    ) -> None:
        self._incident_service = incident_service
        self._pause_execution = pause_execution
        self._recoverable_timeouts = recoverable_timeouts
        self._sim_pacer_for = sim_pacer_for

    @property
    def recoverable_timeout_coordinator(self) -> RecoverableTimeoutCoordinator:
        return self._recoverable_timeouts

    def sim_pacer_for(self, execution_id: str) -> SimPacer | None:
        return self._sim_pacer_for(execution_id)

    def declare_unresolvable_deadlock(
        self,
        execution_id: str,
        context: UnresolvableDeadlockContext,
    ) -> SystemIncident:
        """Record an UNRESOLVABLE_DEADLOCK incident and pause every thread in the execution.

        **Auto-invoked.** `SystemRuntime.__init__` passes this object to
        `self._system.set_thread_incident_declarer(...)` so every
        `ExecutingLabwareThread` minted by the factory holds a
        back-reference and invokes this method from its typed
        `except UnresolvableDeadlockError:` catch (see
        `executing_labware_thread.py` near the action-resolution call).

        On invocation:
          1. Records one incident under
             `IncidentCategory.UNRESOLVABLE_DEADLOCK` with the rich
             diagnostic context (requester / blocker thread + labware
             ids, location, reason, hint). Surfaces on every operator
             surface that consumes the IncidentService:
             `orca incidents list` (CLI), `GET /api/incidents` (REST).
          2. Pauses the whole execution via `pause_execution`: every
             thread parks at its next safe point AND the execution-level
             latch refuses JOIN_EXISTING submissions until resume, same
             as a detected stall. The raising thread itself still goes
             through `_pause_for_error` for the error-state transition.
        """
        # `UnresolvableDeadlockContext` is the IncidentDetail union member
        # directly -- no separate persistence dataclass. The engine error
        # and the persisted record share one shape so adding fields to one
        # propagates automatically to the other.
        incident = self._incident_service.record(
            category=IncidentCategory.UNRESOLVABLE_DEADLOCK,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Unresolvable deadlock: thread "
                f"'{context.requesting_thread_id}' blocked on "
                f"'{context.blocking_position_id}' by immovable thread "
                f"'{context.blocking_thread_id}'."
            ),
            detail=context,
            recovery_action=RecoveryAction.THREAD_RECOVER_ABORT,
            execution_id=execution_id,
            thread_id=context.requesting_thread_id,
        )
        self._pause_execution(execution_id, reason="system", message=incident.message)
        return incident

    def declare_action_failure(
        self,
        execution_id: str,
        thread_id: str,
        context: ActionFailedContext,
    ) -> SystemIncident:
        """Record an ACTION_FAILED incident for a default-PAUSE action error.

        Auto-invoked from ``ExecutingLabwareThread._handle_action_error``
        on the PAUSE branch, right after the thread pauses itself and
        before it parks for an operator recovery decision. The thread is
        already PAUSED with ``last_error`` set; this adds the queryable
        record so the failure surfaces on ``orca incident list`` /
        ``GET /api/incidents`` -- it does NOT
        fan out a pause (the single thread paused itself; other threads
        keep running).

        Recovery is the existing ``orca execution thread recover`` path, and the suggested
        verb follows ``context.device_command``. A thread suspended at one step
        inside the action (a device call, or a wait that ran out of time) is
        advised ``THREAD_RECOVER_RETRY``, which repeats that step. A failure in
        the action's own code
        is advised ``THREAD_RECOVER_RETRY_ACTION``, which is the only retry the
        runtime accepts there.
        """
        inside_call = (
            f" at '{context.device_command}'"
            if context.device_command is not None else ""
        )
        return self._incident_service.record(
            category=IncidentCategory.ACTION_FAILED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Action '{context.action_command}' in method "
                f"'{context.method_name}' failed{inside_call}: "
                f"{context.error_type}: {context.error_message}"
            ),
            detail=context,
            recovery_action=(
                RecoveryAction.THREAD_RECOVER_RETRY
                if context.device_command is not None
                else RecoveryAction.THREAD_RECOVER_RETRY_ACTION
            ),
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_co_labware_timeout(
        self,
        execution_id: str,
        thread_id: str,
        context: CoLabwareTimeoutDetail,
    ) -> SystemIncident:
        """Record a CO_LABWARE_TIMEOUT incident; the thread has paused and RETRY waits again."""
        return self._incident_service.record(
            category=IncidentCategory.CO_LABWARE_TIMEOUT,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Waited {context.waited_seconds:.1f}s at '{context.position_id}' for "
                f"{', '.join(context.missing_labware_names) or 'labware'}; RETRY waits again."
            ),
            detail=context,
            recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_auto_spawn_failure(
        self,
        execution_id: str,
        context: AutoSpawnFailedDetail,
        error_message: str,
    ) -> SystemIncident:
        """Record an AUTO_SPAWN_FAILED incident; the requesting thread pauses, and RETRY spawns again."""
        return self._incident_service.record(
            category=IncidentCategory.AUTO_SPAWN_FAILED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Could not spawn the thread bringing '{context.requested_labware_name}' to thread "
                f"{context.requesting_thread_id}'s action: {error_message}. Fix what stopped it, then RETRY the paused thread."
            ),
            detail=context,
            recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
            execution_id=execution_id,
            thread_id=context.requesting_thread_id,
        )

    def declare_thread_death(
        self,
        execution_id: str,
        thread_id: str,
        context: ThreadDiedContext,
    ) -> SystemIncident:
        """Record a THREAD_DIED incident for a thread that stopped uncaught.

        Reaching here means the error escaped the thread's own handling. The
        thread pauses at UNHANDLED_ERROR, where only ABORT_THREAD applies, or has
        failed when the error ended it by the author's ABORT policy. Either way
        its labware sits where the crash left it. The incident names that
        position, because clearing it by hand is the only way it moves. No recovery verb repairs the step, so none is advised.
        """
        where = (
            f" at {context.last_position_id}"
            if context.last_position_id is not None else ""
        )
        return self._incident_service.record(
            category=IncidentCategory.THREAD_DIED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Thread '{context.thread_name}' stopped on an uncaught "
                f"{context.error_type}: {context.error_message}. Its labware "
                f"'{context.labware_name}' is{where or ' wherever it stopped'} "
                f"and will not move on its own."
            ),
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_device_init_failure(
        self,
        execution_id: str | None,
        error: DeviceInitializationError,
        *,
        tolerated: bool = False,
    ) -> SystemIncident:
        """Record a DEVICE_INIT_FAILED incident for a device that would not come up.

        With no execution the run was refused (RESUBMIT), or ran without this pool member (``tolerated``, a warning).
        A failed execution's bring-up suggests RESTART_EXECUTION.
        """
        if tolerated:
            severity, recovery = IncidentSeverity.WARNING, RecoveryAction.NONE
        elif execution_id is None:
            severity, recovery = IncidentSeverity.ERROR, RecoveryAction.RESUBMIT
        else:
            severity, recovery = IncidentSeverity.ERROR, RecoveryAction.RESTART_EXECUTION
        return self._incident_service.record(
            category=IncidentCategory.DEVICE_INIT_FAILED,
            severity=severity,
            message=(
                f"Device '{error.device_name}' failed to initialize: "
                f"{error.cause}"
            ),
            detail=DeviceInitFailedDetail(
                device_name=error.device_name,
                driver_error=str(error.cause),
            ),
            recovery_action=recovery,
            execution_id=execution_id,
        )

    def declare_action_continued(
        self,
        execution_id: str,
        thread_id: str,
        context: ActionContinuedContext,
    ) -> None:
        """Record ACTION_CONTINUED when an operator carries on past an errored action.

        The ACTION_FAILED incident for the same action stays unacknowledged:
        continuing is not resolving. This one says what the operator did and
        that nothing has re-checked the world model against the device since,
        which is why the severity is WARNING and no recovery action applies --
        there is nothing left to recover, only something to be aware of.
        """
        unsaved = (
            "" if context.unsaved_command is None else
            f" '{context.unsaved_command}' is not on the record. Its arguments and the device's answer are on this"
            f" incident as unsaved_request and unsaved_result, so the record can be put right."
        )
        self._incident_service.record(
            category=IncidentCategory.ACTION_CONTINUED,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Operator continued past errored action "
                f"'{context.action_command}' in method '{context.method_name}' "
                f"({context.error_type}: {context.error_message}). The action's "
                f"side effects are unknown; state past this point is unverified.{unsaved}"
            ),
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_move_failure(
        self,
        execution_id: str,
        thread_id: str,
        context: MoveFailedContext,
    ) -> None:
        """Record a MOVE_FAILED incident for a default-PAUSE routing move error.

        The move-side mirror of ``declare_action_failure``: auto-invoked from
        ``ExecutingLabwareThread._execute_move_action`` on the PAUSE branch,
        after the thread pauses itself. Adds the queryable record; does NOT fan
        out a pause. Recovery is the existing ``orca execution thread recover`` retry.

        Always ``THREAD_RECOVER_RETRY``: at a move pause RETRY repeats the
        move, and RETRY_ACTION is refused because a move is not an action.
        """
        self._incident_service.record(
            category=IncidentCategory.MOVE_FAILED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Move of '{context.labware}' from '{context.source}' to "
                f"'{context.target}' via '{context.transporter}' failed: "
                f"{context.error_type}: {context.error_message}"
            ),
            detail=context,
            recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_move_continued(
        self,
        execution_id: str,
        thread_id: str,
        context: MoveContinuedContext,
    ) -> None:
        """Record MOVE_CONTINUED when an operator finishes a failed move by hand.

        The MOVE_FAILED incident for the same move stays as it is: the arm did
        fail. This one says the labware reached the target by hand, so an
        operator reading incidents later sees which arrival rests on a person's
        word rather than on a completed place.
        """
        self._incident_service.record(
            category=IncidentCategory.MOVE_CONTINUED,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Operator finished the move of '{context.labware}' from "
                f"'{context.source}' to '{context.target}' by hand after "
                f"'{context.transporter}' failed ({context.error_type}: "
                f"{context.error_message}). The run continues from the target."
            ),
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_unresolved_anchor_insert(
        self,
        execution_id: str,
        thread_id: str,
        anchor_name: str,
        direction: str,
        target_type: str,
        item_name: str | None,
        anchor_reached: bool,
    ) -> SystemIncident:
        """Record a WARNING UNRESOLVED_ANCHOR_INSERT incident for a dropped insert.

        Fired when a ``Before``/``After`` insert is still pending at lane
        close. ``anchor_reached`` says which drop it was: the anchor name
        never came past, or it did and the insert still never ran. Both are
        expected (conditional methods/actions skip anchors; an aborted thread
        ends with inserts still queued; an operator can anchor to a step the
        lane has gone by), so this is informational, not recoverable -- it
        only makes the otherwise-silent drop queryable.
        """
        return self._incident_service.record(
            category=IncidentCategory.UNRESOLVED_ANCHOR_INSERT,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Dropped {direction}-anchor insert of {target_type} "
                f"'{item_name or '<unnamed>'}': "
                + (
                    f"'{anchor_name}' did appear, but the insert never ran"
                    if anchor_reached
                    else f"anchor '{anchor_name}' never appeared on the stream"
                )
            ),
            detail=UnresolvedAnchorInsertDetail(
                anchor_name=anchor_name,
                direction=direction,
                target_type=target_type,
                item_name=item_name or "<unnamed>",
                anchor_reached=anchor_reached,
            ),
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_orphaned_backlog(
        self,
        execution_id: str,
        context: OrphanedBacklogContext,
    ) -> None:
        """Record an ORPHANED_BACKLOG incident for a quarantined slot.

        The workflow already flagged the slot and pause-requested the
        in-scope threads; this adds the queryable record. Recovery is the
        EXECUTION-level resume (accept-partial): the orphaned contributions
        are abandoned and their threads continue. Per-thread resume and the
        recoverable-timeout resume path do not dispose of the backlog, and
        JOIN_EXISTING submissions are refused until the resume.
        """
        in_flight = (
            f" plus in-flight method '{context.in_flight_method_name}'"
            if context.in_flight_method_name is not None
            else ""
        )
        self._incident_service.record(
            category=IncidentCategory.ORPHANED_BACKLOG,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Receiver '{context.receiver_thread_name}' "
                f"({context.receiver_status}) died still owing work on slot "
                f"'{context.slot_key}': {context.undelivered_count} "
                f"undelivered contribution(s){in_flight}. The slot is "
                f"quarantined and the affected threads are pausing. Resume "
                f"the EXECUTION to accept the partial fill: undelivered "
                f"contributions are abandoned and their threads continue; "
                f"work already executing or under error recovery finishes "
                f"on its own channel."
            ),
            detail=context,
            recovery_action=RecoveryAction.RESUME_EXECUTION,
            execution_id=execution_id,
            thread_id=context.receiver_thread_id,
        )
