"""One list of what is stopping the run.

Recovery state lives in several places -- a thread's pause fields, a device's
fault record, an execution's pause latch, a reservation nobody will release, a
park waiting on an operator. Each has its own read and its own verbs, so
clearing one says nothing about the rest. An operator clears a device fault and
expects the arm to move; the thread it faulted is still paused and nothing told
them.

A blocker is one condition that stops work, will not clear on its own, and
carries the remedies that clear it. The list is derived from that state on
every read, so it cannot drift from the runtime the way a queue would.

An unacknowledged incident is not a blocker. It is the record of one, and
counting both would report every error pause twice. A blocker carries its
``incident_id`` instead.

The verbs offered at a pause are the ones the thread says it will accept, read
off its snapshot. The runtime derives them from the same rule that refuses a
verb on the call, so a verb can never be offered here and refused there.
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol

from orca.runtime.incident_store import IncidentCategory, SystemIncident
from orca.runtime.remedies import Remedy, RemedyContext, RemedyStep, remedies_for
from orca.runtime.status_models import (
    DeviceSnapshot,
    ExecutionDetail,
    PendingManualStepRecord,
    ReservationSnapshot,
    ThreadSnapshot,
)
from orca.workflow_models.status_enums import (
    ESCALATION_ORDER,
    LabwareThreadStatus,
    PauseSite,
    RecoveryDecision,
)


class BlockerKind(str, Enum):
    """What kind of thing is stopping the run."""

    DEVICE_FAULT = "DEVICE_FAULT"
    DEVICE_EXTERNAL_CONTROL = "DEVICE_EXTERNAL_CONTROL"
    THREAD_ERROR_PAUSE = "THREAD_ERROR_PAUSE"
    EXECUTION_PAUSED = "EXECUTION_PAUSED"
    WAITING_ON_A_PARKED_HOLDER = "WAITING_ON_A_PARKED_HOLDER"
    MANUAL_PLACE = "MANUAL_PLACE"
    MANUAL_REMOVE = "MANUAL_REMOVE"
    MANUAL_STEP = "MANUAL_STEP"
    RUNTIME_NOT_BUILT = "RUNTIME_NOT_BUILT"


class BlockerSeverity(str, Enum):
    """Whether something is broken, or a person simply has to do the thing."""

    ERROR = "error"
    WARNING = "warning"


_VERB_LABELS: dict[RecoveryDecision, str] = {
    RecoveryDecision.RETRY: "Retry the whole action",
    RecoveryDecision.RETRY_OP: "Retry just the device call",
    RecoveryDecision.CONTINUE: "I dealt with it, carry on",
    RecoveryDecision.ABORT_ACTION: "Drop this action",
    RecoveryDecision.ABORT_METHOD: "Drop the rest of this method",
    RecoveryDecision.ABORT_THREAD: "End this plate's journey",
}

_VERB_EXPLAIN: dict[RecoveryDecision, str] = {
    RecoveryDecision.RETRY: (
        "Re-runs the action body from the top, so every device call it already "
        "completed runs a second time."
    ),
    RecoveryDecision.RETRY_OP: (
        "Re-runs only the call that failed. The body stays parked and the work "
        "it already did stays done."
    ),
    RecoveryDecision.CONTINUE: (
        "Says a person has dealt with it and the run may proceed. Records that "
        "state past this point is unverified."
    ),
    RecoveryDecision.ABORT_ACTION: "Discards this action as work that did not happen.",
    RecoveryDecision.ABORT_METHOD: "Abandons the rest of this method.",
    RecoveryDecision.ABORT_THREAD: (
        "Ends this plate's journey here. The execution finishes at ABORTED, "
        "not COMPLETED."
    ),
}

_PARKED_STATUSES = frozenset({
    LabwareThreadStatus.PAUSED.value,
    LabwareThreadStatus.AWAITING_MANUAL_PLACE.value,
    LabwareThreadStatus.AWAITING_MANUAL_REMOVE.value,
})

_RESERVATION_WAIT_STATUSES = frozenset({
    LabwareThreadStatus.AWAITING_ACTION_RESERVATION.value,
    LabwareThreadStatus.AWAITING_MOVE_RESERVATION.value,
    LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY.value,
})

_SYSTEM_PAUSE_CATEGORIES = frozenset({
    IncidentCategory.SYSTEM_STALL,
    IncidentCategory.ORPHANED_BACKLOG,
    IncidentCategory.UNRESOLVABLE_DEADLOCK,
    IncidentCategory.RESERVATION_DEADLOCK,
})


@dataclass(frozen=True)
class Blocker:
    """One thing stopping the run, with the remedies that clear it."""

    id: str
    kind: BlockerKind
    severity: BlockerSeverity
    since: float | None
    """When this started, unix seconds. None when nothing recorded it."""
    headline: str
    remedies: tuple[Remedy, ...]
    detail: str | None = None
    device_name: str | None = None
    execution_id: str | None = None
    thread_id: str | None = None
    labware_id: str | None = None
    incident_id: str | None = None
    may_still_be_moving: bool = False
    """Set on a device fault whose command never answered. Nothing told the
    instrument to stop, so it may be running right now."""
    blocks: tuple[str, ...] = field(default_factory=tuple)
    """Ids of blockers that clear when this one does.

    Only ever set from the reservation graph, where the link is a fact: this
    blocker's thread holds a reservation another thread is waiting on. Nothing
    is inferred from error text.
    """


class BlockerSources(Protocol):
    """The reads the derivation needs. ``SystemRuntime`` satisfies it."""

    async def blocker_device_snapshots(self) -> list[DeviceSnapshot]: ...
    def blocker_execution_details(self) -> list[ExecutionDetail]: ...
    def blocker_pending_manual_steps(self) -> list[PendingManualStepRecord]: ...
    def blocker_reservations(self) -> list[ReservationSnapshot]: ...
    async def blocker_open_incidents(self) -> list[SystemIncident]: ...


_KIND_ORDER: dict[BlockerKind, int] = {
    BlockerKind.RUNTIME_NOT_BUILT: 0,
    BlockerKind.DEVICE_FAULT: 1,
    BlockerKind.THREAD_ERROR_PAUSE: 2,
    BlockerKind.EXECUTION_PAUSED: 3,
    BlockerKind.WAITING_ON_A_PARKED_HOLDER: 4,
    BlockerKind.MANUAL_PLACE: 5,
    BlockerKind.MANUAL_REMOVE: 6,
    BlockerKind.MANUAL_STEP: 7,
    BlockerKind.DEVICE_EXTERNAL_CONTROL: 8,
}


def _sort_key(blocker: Blocker) -> tuple[int, int, bool, float]:
    # A machine that may still be moving outranks everything else of its kind.
    return (
        _KIND_ORDER[blocker.kind],
        0 if blocker.may_still_be_moving else 1,
        blocker.since is None,
        blocker.since or 0.0,
    )


def _one_step(step: RemedyStep, explain: str, recommended: bool = False,
              confirm: bool = False) -> Remedy:
    """A bare verb, as a one-step remedy so there is one concept, not two."""
    return Remedy(
        id=step.verb,
        label=step.label,
        explain=explain,
        steps=(step,),
        recommended=recommended,
        confirm=confirm,
    )


def _recover_step(
    decision: RecoveryDecision, execution_id: str, thread_id: str,
) -> RemedyStep:
    return RemedyStep(
        verb=f"thread.recover.{decision.value}",
        label=_VERB_LABELS[decision],
        args={
            "execution_id": execution_id,
            "thread_id": thread_id,
            "decision": decision.value,
        },
        mcp_tool="operations_recover_thread",
        rest="POST /api/operations/recover-thread",
        cli=(
            f"orca thread recover {execution_id} {thread_id} "
            f"--decision {decision.value}"
        ),
    )


def _bare_verb_remedies(
    thread: ThreadSnapshot, execution_id: str, any_named: bool,
) -> tuple[Remedy, ...]:
    """Only the verbs this pause site honours.

    Sending a verb the site does not honour is not a no-op: at a move it fails
    the thread and takes the execution down. The site is known for certain
    here, so nobody downstream has to hold the table.
    """
    if thread.pause_site is None:
        return ()
    site = PauseSite(thread.pause_site)
    out: list[Remedy] = []
    # The thread already carries what it will accept. Deriving it again here
    # would be a second answer to one question.
    for decision in sorted(
        (RecoveryDecision(d) for d in thread.honoured_decisions),
        key=ESCALATION_ORDER.index,
    ):
        if decision is RecoveryDecision.RETRY_OP and thread.paused_device_command is None:
            continue
        explain = _VERB_EXPLAIN[decision]
        if decision is RecoveryDecision.CONTINUE and site is PauseSite.MOVE:
            explain = (
                "Accepted only once the ledger puts the plate at the move's "
                "target. Record the position first."
            )
        recommend = not any_named and _is_default_verb(decision, thread)
        out.append(
            _one_step(
                _recover_step(decision, execution_id, thread.id),
                explain,
                recommended=recommend,
                confirm=decision is not RecoveryDecision.RETRY,
            ),
        )
    return tuple(out)


def _is_default_verb(decision: RecoveryDecision, thread: ThreadSnapshot) -> bool:
    """The retry that fits, when no named remedy claimed the failure.

    ``paused_device_command`` is the condition the runtime judges RETRY_OP on,
    so a non-null value means the op-level retry is the one that will be
    accepted and the one that does not re-run work the body already did.
    """
    if thread.paused_device_command is not None:
        return decision is RecoveryDecision.RETRY_OP
    return decision is RecoveryDecision.RETRY


def _device_fault_blocker(device: DeviceSnapshot) -> Blocker:
    fault = device.fault
    assert fault is not None
    clear = RemedyStep(
        verb="device.clear_fault",
        label="Clear the fault",
        args={"device_name": device.name},
        mcp_tool="operations_clear_device_fault",
        rest="POST /api/operations/clear-device-fault",
        cli=f"orca device clear-fault {device.name}",
    )
    remedies: list[Remedy] = []
    if fault.may_still_be_moving:
        remedies.append(
            Remedy(
                id="look_before_clearing",
                label="Look at the machine, then clear",
                explain=(
                    "No answer came back, so nothing told the instrument to "
                    "stop and it may still be moving. Look before you touch it "
                    "or send anything else. Compare the deck to find out where "
                    "the plate ended up, record it, then clear."
                ),
                recommended=True,
                confirm=True,
                steps=(
                    RemedyStep(
                        verb="device.compare_deck",
                        label="See where the plate actually is",
                        args={"device_name": device.name},
                        mcp_tool="operations_compare_deck",
                        rest="POST /api/operations/compare-deck",
                        cli=f"orca device compare-deck {device.name}",
                    ),
                    RemedyStep(
                        verb="labware.edit_location",
                        label="Record where the plate is",
                        args={},
                        mcp_tool="operations_edit_labware_location",
                        rest="POST /api/operations/edit-labware-location",
                        cli="orca labware edit-location <labware_id> <location>",
                        needs=("labware_id", "location", "reason"),
                    ),
                    clear,
                ),
            ),
        )
    remedies.append(
        _one_step(
            clear,
            "Clears the record, not the trouble. Put the instrument right first.",
            recommended=not fault.may_still_be_moving,
            confirm=True,
        ),
    )
    return Blocker(
        id=f"device_fault:{device.name}",
        kind=BlockerKind.DEVICE_FAULT,
        severity=BlockerSeverity.ERROR,
        since=fault.at,
        headline=f"{device.name}: {fault.command!r} did not come back clean.",
        detail=fault.message,
        device_name=device.name,
        execution_id=fault.execution_id,
        may_still_be_moving=fault.may_still_be_moving,
        remedies=tuple(remedies),
    )


def _external_control_blocker(device: DeviceSnapshot) -> Blocker:
    hold = device.external_control_hold or "no reason given"
    return Blocker(
        id=f"device_external_control:{device.name}",
        kind=BlockerKind.DEVICE_EXTERNAL_CONTROL,
        severity=BlockerSeverity.WARNING,
        # The claim carries no timestamp, so ordering falls back to kind.
        since=None,
        headline=f"{device.name} is held for hands-on work ({hold}).",
        detail=(
            "The workflow cannot drive it until the hold is released. Your own "
            "commands still reach it."
        ),
        device_name=device.name,
        remedies=(
            _one_step(
                RemedyStep(
                    verb="device.release_external_control",
                    label="Give it back to the workflow",
                    args={"device_name": device.name},
                    mcp_tool="operations_release_device_control",
                    rest="POST /api/operations/release-device-control",
                    cli=f"orca device release-control {device.name}",
                ),
                "The workflow can drive it again from the next dispatch.",
                recommended=True,
            ),
        ),
    )


def _incident_for_thread(
    incidents: list[SystemIncident], execution_id: str, thread_id: str,
) -> SystemIncident | None:
    for incident in incidents:
        if incident.execution_id == execution_id and incident.thread_id == thread_id:
            return incident
    return None


def _incident_for_execution(
    incidents: list[SystemIncident], execution_id: str,
) -> SystemIncident | None:
    for incident in incidents:
        if (
            incident.execution_id == execution_id
            and incident.category in _SYSTEM_PAUSE_CATEGORIES
        ):
            return incident
    return None


def _error_type_of(incident: SystemIncident | None) -> str | None:
    """The exception class behind an incident, when its detail carries one."""
    if incident is None:
        return None
    return getattr(incident.detail, "error_type", None)


def _thread_error_blocker(
    thread: ThreadSnapshot,
    detail: ExecutionDetail,
    incidents: list[SystemIncident],
) -> Blocker:
    incident = _incident_for_thread(incidents, detail.id, thread.id)
    at_move = thread.pause_site == PauseSite.MOVE.value
    named = remedies_for(
        _error_type_of(incident),
        RemedyContext(
            execution_id=detail.id,
            thread_id=thread.id,
            labware_id=thread.labware_id,
            labware_name=thread.labware_name,
            paused_device_command=thread.paused_device_command,
        ),
        at_move=at_move,
    )
    said = thread.pause_message or thread.last_error or "no detail recorded"
    return Blocker(
        id=f"thread_pause:{detail.id}:{thread.id}",
        kind=BlockerKind.THREAD_ERROR_PAUSE,
        severity=BlockerSeverity.ERROR,
        since=incident.timestamp if incident is not None else None,
        headline=f"{thread.name} stopped: {said}",
        detail=thread.last_error,
        execution_id=detail.id,
        thread_id=thread.id,
        labware_id=thread.labware_id,
        incident_id=incident.id if incident is not None else None,
        remedies=named + _bare_verb_remedies(
            thread, detail.id, any_named=any(r.recommended for r in named),
        ),
    )


def _manual_park_blocker(
    thread: ThreadSnapshot, detail: ExecutionDetail,
) -> Blocker | None:
    if thread.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.value:
        return Blocker(
            id=f"manual_place:{detail.id}:{thread.id}",
            kind=BlockerKind.MANUAL_PLACE,
            severity=BlockerSeverity.WARNING,
            since=None,
            headline=(
                f"Put {thread.labware_template_name or thread.name} on "
                f"{thread.waiting_for}."
            ),
            execution_id=detail.id,
            thread_id=thread.id,
            labware_id=thread.labware_id,
            remedies=(
                _one_step(
                    RemedyStep(
                        verb="labware.register",
                        label="I placed it",
                        args={
                            "template_name": thread.labware_template_name,
                            "location": thread.waiting_for,
                        },
                        mcp_tool="operations_register_labware",
                        rest="POST /api/operations/register-labware",
                        cli=(
                            f"orca labware register "
                            f"{thread.labware_template_name} "
                            f"--location {thread.waiting_for}"
                        ),
                    ),
                    "Binds the plate you put down to the thread already waiting "
                    "for it, and the thread carries on.",
                    recommended=True,
                ),
            ),
        )
    if thread.status == LabwareThreadStatus.AWAITING_MANUAL_REMOVE.value:
        return Blocker(
            id=f"manual_remove:{detail.id}:{thread.id}",
            kind=BlockerKind.MANUAL_REMOVE,
            severity=BlockerSeverity.WARNING,
            since=None,
            headline=(
                f"Take {thread.labware_name or thread.name} off {thread.waiting_for}."
            ),
            execution_id=detail.id,
            thread_id=thread.id,
            labware_id=thread.labware_id,
            remedies=(
                _one_step(
                    RemedyStep(
                        verb="labware.discharge",
                        label="I took it off",
                        args={"labware_id": thread.labware_id},
                        mcp_tool="operations_discharge_labware",
                        rest="POST /api/operations/discharge-labware",
                        cli=f"orca labware discharge {thread.labware_id}",
                    ),
                    "Closes the plate out and releases the park. Its record "
                    "stays queryable.",
                    recommended=True,
                ),
            ),
        )
    return None


def _execution_paused_blocker(
    detail: ExecutionDetail, incidents: list[SystemIncident],
) -> Blocker:
    incident = _incident_for_execution(incidents, detail.id)
    who = "The system" if detail.pause_reason == "system" else "An operator"
    headline = f"{who} paused {detail.workflow_name}."
    if incident is not None:
        headline = f"{detail.workflow_name} paused: {incident.message}"
    return Blocker(
        id=f"execution_paused:{detail.id}",
        kind=BlockerKind.EXECUTION_PAUSED,
        severity=BlockerSeverity.ERROR,
        since=incident.timestamp if incident is not None else None,
        headline=headline,
        detail=(
            "A second confirmed stop would abort this run."
            if detail.abort_armed
            else None
        ),
        execution_id=detail.id,
        incident_id=incident.id if incident is not None else None,
        remedies=(
            _one_step(
                RemedyStep(
                    verb="execution.resume",
                    label="Resume the run",
                    args={"execution_id": detail.id},
                    mcp_tool="operations_resume",
                    rest="POST /api/operations/resume",
                    cli=f"orca execution resume {detail.id}",
                ),
                "Every thread picks up where it stopped.",
                recommended=True,
            ),
        ),
    )


def _manual_step_blocker(record: PendingManualStepRecord) -> Blocker:
    return Blocker(
        id=f"manual_step:{record.execution_id}:{record.step_id}",
        kind=BlockerKind.MANUAL_STEP,
        severity=BlockerSeverity.WARNING,
        since=record.emitted_at.timestamp(),
        headline=record.instruction,
        execution_id=record.execution_id,
        remedies=(
            _one_step(
                RemedyStep(
                    verb="manual_step.confirm",
                    label="Done",
                    args={
                        "execution_id": record.execution_id,
                        "step_id": record.step_id,
                    },
                    mcp_tool="operations_confirm_manual_step",
                    rest="POST /api/operations/confirm-manual-step",
                    cli=(
                        f"orca manual-step confirm {record.execution_id} "
                        f"{record.step_id}"
                    ),
                ),
                "The thread carries on from the step.",
                recommended=True,
            ),
        ),
    )


def runtime_not_built_blocker(
    error_type: str, message: str, hint: str, since: float | None = None,
) -> Blocker:
    """The one blocker that exists when there is no runtime to ask."""
    return Blocker(
        id="runtime_not_built",
        kind=BlockerKind.RUNTIME_NOT_BUILT,
        severity=BlockerSeverity.ERROR,
        since=since,
        headline=f"The runtime is not built: {error_type}: {message}",
        detail=hint,
        remedies=(
            _one_step(
                RemedyStep(
                    verb="runtime.reload",
                    label="Rebuild after fixing the file",
                    args={},
                    mcp_tool="runtime_reload",
                    rest="POST /api/runtime/reload",
                    cli="",
                ),
                "Re-imports the deployment package. Fix the file the message "
                "names first.",
                recommended=True,
            ),
        ),
    )


def runtime_torn_down_blocker(since: float | None = None) -> Blocker:
    """No runtime, and nothing recorded a build failure.

    A teardown rather than a broken file: a topology delete does this on
    purpose. Reporting an empty list here would say the way is clear on a
    deployment that cannot run anything at all.
    """
    return Blocker(
        id="runtime_not_built",
        kind=BlockerKind.RUNTIME_NOT_BUILT,
        severity=BlockerSeverity.ERROR,
        since=since,
        headline="There is no runtime. Nothing can run until one is built.",
        detail=(
            "The runtime was torn down or never started, and no build error "
            "was recorded. Submit a topology, or reload to build from what is "
            "already on disk."
        ),
        remedies=(
            _one_step(
                RemedyStep(
                    verb="runtime.reload",
                    label="Build from what is on disk",
                    args={},
                    mcp_tool="runtime_reload",
                    rest="POST /api/runtime/reload",
                    cli="",
                ),
                "Imports the deployment package as it stands. Needs a "
                "system.py already in the worktree.",
                recommended=True,
            ),
        ),
    )


def _starved_waiter_blockers(
    details: list[ExecutionDetail],
    reservations: list[ReservationSnapshot],
) -> tuple[list[Blocker], dict[str, list[str]]]:
    """Threads spinning on a reservation whose holder is itself parked.

    Nothing is error-paused in this shape and no incident exists, so every
    other read reports a healthy run that is quietly going nowhere. The
    holder-is-parked check is what makes it a blocker rather than ordinary
    contention: a thread that is still acting will release on its own, and
    routine waits legitimately last hours.
    """
    parked: dict[str, tuple[ThreadSnapshot, ExecutionDetail]] = {}
    for detail in details:
        for thread in detail.threads:
            if thread.status in _PARKED_STATUSES:
                parked[thread.id] = (thread, detail)
    if not parked:
        return [], {}

    held_by_position: dict[str, ReservationSnapshot] = {
        r.position_id: r for r in reservations if r.thread_id in parked
    }
    if not held_by_position:
        return [], {}

    out: list[Blocker] = []
    waiters_by_holder: dict[str, list[str]] = {}
    for detail in details:
        for thread in detail.threads:
            if thread.status not in _RESERVATION_WAIT_STATUSES:
                continue
            if thread.waiting_for is None:
                continue
            wanted = [name.strip() for name in thread.waiting_for.split(",")]
            reservation = next(
                (held_by_position[name] for name in wanted if name in held_by_position),
                None,
            )
            if reservation is None or reservation.thread_id is None:
                continue
            holder, holder_detail = parked[reservation.thread_id]
            blocker = _starved_waiter_blocker(
                thread, detail, holder, holder_detail, reservation,
            )
            out.append(blocker)
            waiters_by_holder.setdefault(reservation.thread_id, []).append(blocker.id)
    return out, waiters_by_holder


def _starved_waiter_blocker(
    waiter: ThreadSnapshot,
    waiter_detail: ExecutionDetail,
    holder: ThreadSnapshot,
    holder_detail: ExecutionDetail,
    reservation: ReservationSnapshot,
) -> Blocker:
    return Blocker(
        id=f"starved:{waiter_detail.id}:{waiter.id}",
        kind=BlockerKind.WAITING_ON_A_PARKED_HOLDER,
        severity=BlockerSeverity.ERROR,
        since=None,
        headline=(
            f"{waiter.name} is waiting on {reservation.position_id}, which "
            f"{holder.name} is holding while parked."
        ),
        detail=(
            f"{holder.name} is {holder.status} and will not release "
            f"{reservation.position_id} on its own. Deal with that thread and "
            f"this one moves. Cancelling the hold instead is an override: if "
            f"{holder.name} still needs the position it has to reserve it again."
        ),
        execution_id=waiter_detail.id,
        thread_id=waiter.id,
        labware_id=waiter.labware_id,
        remedies=(
            _one_step(
                RemedyStep(
                    verb="reservation.cancel",
                    label="Release the hold",
                    args={
                        "execution_id": holder_detail.id,
                        "reservation_id": reservation.reservation_id,
                    },
                    mcp_tool="reservation_cancel",
                    rest=(
                        f"DELETE /api/executions/{holder_detail.id}"
                        f"/reservations/{reservation.reservation_id}"
                    ),
                    cli=(
                        f"orca reservation cancel {holder_detail.id} "
                        f"{reservation.reservation_id}"
                    ),
                    needs=("reason",),
                    query=("reason",),
                ),
                f"Takes {reservation.position_id} back from {holder.name} so "
                f"this thread can proceed. Do this when the holder has no "
                f"further need of it.",
                confirm=True,
            ),
        ),
    )


def _link_holders_to_waiters(
    blockers: list[Blocker], waiters_by_holder_thread: dict[str, list[str]],
) -> list[Blocker]:
    """Say on the holder's own blocker which waiters clear when it does.

    This is the link an operator otherwise has to work out: two rows that look
    unrelated, where dealing with the first releases the second.
    """
    if not waiters_by_holder_thread:
        return blockers
    return [
        replace(b, blocks=tuple(waiters_by_holder_thread[b.thread_id]))
        if b.thread_id is not None
        and b.thread_id in waiters_by_holder_thread
        and b.kind is not BlockerKind.WAITING_ON_A_PARKED_HOLDER
        else b
        for b in blockers
    ]


async def derive_blockers(sources: BlockerSources) -> list[Blocker]:
    """Everything stopping the run right now, worst first.

    A system pause rolls up to ONE execution row rather than one per paused
    thread: those threads were parked by the same stall or quarantine and take
    one decision between them. Only error pauses get a row each, because each
    carries its own verbs.
    """
    incidents = await sources.blocker_open_incidents()
    blockers: list[Blocker] = []

    for device in await sources.blocker_device_snapshots():
        if device.fault is not None:
            blockers.append(_device_fault_blocker(device))
        if device.under_external_control and device.external_control_hold is not None:
            blockers.append(_external_control_blocker(device))

    details = sources.blocker_execution_details()
    for detail in details:
        for thread in detail.threads:
            if thread.pause_reason == "error":
                blockers.append(_thread_error_blocker(thread, detail, incidents))
                continue
            park = _manual_park_blocker(thread, detail)
            if park is not None:
                blockers.append(park)
        if detail.paused:
            blockers.append(_execution_paused_blocker(detail, incidents))

    starved, waiters_by_holder = _starved_waiter_blockers(
        details, sources.blocker_reservations(),
    )
    blockers.extend(starved)

    for record in sources.blocker_pending_manual_steps():
        blockers.append(_manual_step_blocker(record))

    blockers.sort(key=_sort_key)
    return _link_holders_to_waiters(blockers, waiters_by_holder)
