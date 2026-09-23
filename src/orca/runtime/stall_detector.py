"""Structural stall detection for the persistent runtime.

A co-labware wait (and every other coordination wait) is unbounded by default,
because a co-thread may legitimately run a multi-hour action before it can move
its labware. A wall-clock cap cannot tell that healthy-but-slow wait apart from
a genuine stall, so this detector does not use one. It asks a structural
question instead:

    Is every live thread internally blocked, with none in flight?

If any thread is ``EXECUTING_ACTION`` / ``MOVING`` (or otherwise progressing),
progress is possible and there is no stall -- a multi-hour action keeps its
thread in flight, so the detector never false-positives on a slow-but-healthy
run regardless of how long the tick interval is. The interval only affects how
quickly a genuine stall is surfaced, never whether one is declared.

The stall-candidate wait is specifically ``AWAITING_CO_THREADS``: co-labware is
delivered by a PEER THREAD's action, so if every live thread is waiting for
co-labware and none is acting, nothing will ever deliver it -- a genuine stall.

Status is not the whole answer, because one wait wears another wait's status. A
CONTRIBUTOR parked in a shared action's rendezvous reads ``EXECUTING_ACTION``:
true of the action, false of the thread, which is only awaiting the owner's
outcome. Reading that as in-flight let a single parked contributor mask every
wedge in its execution, so the snapshot carries ``following_peer_action`` and
the detector treats such a thread as another peer-delivered wait rather than as
progress. Any thread whose status genuinely means acting still blocks a verdict,
including the owner it is waiting on, and so does an operator wait the parked
thread has taken on top (a contributor PAUSED by its group's failed action).

Reservation waits (``AWAITING_ACTION_RESERVATION`` / ``AWAITING_MOVE_RESERVATION``
/ ``AWAITING_MOVE_TARGET_AVAILABILITY`` / ``RESOLVING_ACTION_LOCATION``) are
never a stall ALONE: the reservation manager progresses even while every
thread waits, routine contention legitimately lasts minutes to hours (the
physical-timing reality), and genuine reservation cycles are the
reservation-layer deadlock detector's. But a reservation is only ever released
by a thread that acts, so a MIXED stable state -- every live thread in a
co-labware or reservation wait, at least one of each, none in flight -- cannot
self-resolve either: nothing will deliver the co-labware and nothing will
release what the manager could grant. This catches the cross-mechanism wedge
that escapes both single-layer detectors (a co-labware waiter holding a
resource a reservation waiter needs has no edge in the reservation wait-for
graph). The stability window absorbs in-flight grants and transient
all-waiting moments.

The "only an acting thread releases a reservation" premise spans EXECUTIONS:
the holder can be an in-flight thread of a concurrent sibling execution that
releases the device by finishing. Reservation waits therefore join the mixed
rule only while the whole SYSTEM is quiescent (``system_quiescent`` on
``evaluate``, computed across every execution's live threads); under any
in-flight sibling they read as plain contention. Pure co-labware stalls stay
per-execution, so a busy sibling can never mask one.

One residual false-positive shape: the "only a thread releases a reservation"
premise does not hold for SYSTEM-HELD or manual operator holds (reservations
with no owning thread), which an operator can release without any thread
acting. A mixed state blocked on such a hold CAN resolve externally, yet still
trips the verdict. The same bounded shape exists for a holder thread parked in
an operator/external wait (PAUSED, manual place/remove, AWAITING_EVENT) in any
execution: it reads as quiescent here, yet operator input can wake it to
release the blocker. The consequence is bounded and recoverable -- one incident
plus a pause; the operator supplies the input (or releases the hold) and
resumes.

Operator / external waits (``PAUSED``, ``AWAITING_MANUAL_PLACE`` / ``_REMOVE``,
``AWAITING_EVENT``) are legitimate indefinite waits on the outside world and
block detection by design.

Genuine circular co-labware waits and orphaned waits (a contributor that died
without delivering) both reduce to the same observable state -- every live
thread waiting, nothing in flight -- so one rule catches both.
"""
from dataclasses import dataclass

from orca.workflow_models.status_enums import LabwareThreadStatus

# Peer-delivered wait: only another thread's action can resolve it. At least
# one of these must be present for any stall verdict (see module docstring).
_STALL_CANDIDATE_WAITS: frozenset[LabwareThreadStatus] = frozenset({
    LabwareThreadStatus.AWAITING_CO_THREADS,
})

# Manager-granted waits: never a stall alone (plain contention), but they
# cannot resolve without a thread acting, so they join the mixed rule.
_RESERVATION_WAITS: frozenset[LabwareThreadStatus] = frozenset({
    LabwareThreadStatus.AWAITING_ACTION_RESERVATION,
    LabwareThreadStatus.AWAITING_MOVE_RESERVATION,
    LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY,
    LabwareThreadStatus.RESOLVING_ACTION_LOCATION,
})

_INTERNALLY_BLOCKED: frozenset[LabwareThreadStatus] = (
    _STALL_CANDIDATE_WAITS | _RESERVATION_WAITS
)

_TERMINAL: frozenset[LabwareThreadStatus] = frozenset({
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.FAILED,
})

# Operator/external waits: not progress, not internally blocked -- they gate
# detection in their own execution and read as quiescent system-wide.
_EXTERNAL_WAITS: frozenset[LabwareThreadStatus] = frozenset({
    LabwareThreadStatus.PAUSED,
    LabwareThreadStatus.AWAITING_MANUAL_PLACE,
    LabwareThreadStatus.AWAITING_MANUAL_REMOVE,
    LabwareThreadStatus.AWAITING_EVENT,
})


def is_stall_candidate_wait(status: LabwareThreadStatus) -> bool:
    return status in _STALL_CANDIDATE_WAITS


def is_live(status: LabwareThreadStatus) -> bool:
    return status not in _TERMINAL


def is_in_flight(status: LabwareThreadStatus) -> bool:
    """A live thread that is neither internally blocked nor parked on an
    operator/external wait: it is acting and can release reservations."""
    return (
        is_live(status)
        and status not in _INTERNALLY_BLOCKED
        and status not in _EXTERNAL_WAITS
    )


@dataclass(frozen=True)
class ThreadStallSnapshot:
    """One live thread's contribution to a stall evaluation.

    ``following_peer_action`` marks a contributor parked in a shared action's
    rendezvous. Its status is ``EXECUTING_ACTION`` because the action it belongs
    to is the running one, but the OWNER drives the device and this thread only
    awaits the owner's outcome, so it can neither deliver co-labware nor release
    a reservation.
    """
    thread_id: str
    status: LabwareThreadStatus
    waiting_on: str | None = None
    following_peer_action: bool = False


def _parked_on_a_peer(snapshot: ThreadStallSnapshot) -> bool:
    """Is this thread parked on a peer's action with nothing outside to wake it?

    An operator wait outranks the parking. A contributor whose shared action
    failed sits PAUSED awaiting the group's recovery decision, which comes from
    the outside and can move it, so it blocks a verdict like any other external
    wait rather than counting toward one.
    """
    return snapshot.following_peer_action and snapshot.status not in _EXTERNAL_WAITS


def snapshot_in_flight(snapshot: ThreadStallSnapshot) -> bool:
    """Is this thread acting, so it can still release what others wait on?

    Status alone answers this for every thread except a parked contributor,
    whose ``EXECUTING_ACTION`` describes the action rather than the thread.
    """
    return is_in_flight(snapshot.status) and not _parked_on_a_peer(snapshot)


def _is_blocked(snapshot: ThreadStallSnapshot, blocked: frozenset[LabwareThreadStatus]) -> bool:
    return snapshot.status in blocked or _parked_on_a_peer(snapshot)


def _is_candidate(snapshot: ThreadStallSnapshot) -> bool:
    """Peer-delivered wait: only another thread's action can resolve it. A
    parked contributor qualifies for the same reason a co-labware waiter does."""
    return is_stall_candidate_wait(snapshot.status) or _parked_on_a_peer(snapshot)


@dataclass(frozen=True)
class StallReport:
    """A declared stall: the wedged threads and a per-thread wait diagnostic."""
    thread_ids: tuple[str, ...]
    waits: tuple[str, ...]  # one "id [status] waiting on X" line per thread

    @property
    def detail(self) -> str:
        return "; ".join(self.waits)


class SystemStallError(RuntimeError):
    """Raised to an execution AWAITER when the stall detector fires.

    The execution itself stays PAUSED and operator-recoverable (resume clears
    the episode); this error only stops waiters from blocking blind until an
    external timeout."""

    def __init__(self, execution_id: str, report: StallReport) -> None:
        super().__init__(f"System stall in execution {execution_id}: {report.detail}")
        self.execution_id = execution_id
        self.report = report


class StallDetector:
    """Per-execution: one instance evaluates ONE execution's live threads.

    ``evaluate`` is called once per detector tick with that execution's live-thread
    snapshot. It returns a ``StallReport`` ONCE per stall episode: when every live
    thread has been internally blocked (co-labware or reservation wait, with at
    least one co-labware waiter) on the SAME wait for ``required_stable_ticks``
    consecutive calls. Any progress (an in-flight thread, a changed wait subject
    or kind, a thread terminating) resets the counter and re-arms it, so a later
    distinct stall reports again.
    """

    def __init__(self, required_stable_ticks: int = 2) -> None:
        if required_stable_ticks < 1:
            raise ValueError("required_stable_ticks must be >= 1")
        self._required = required_stable_ticks
        self._prev_signature: frozenset[tuple[str, str, str, bool]] | None = None
        self._stable_count = 0
        self._reported = False

    def evaluate(
        self,
        snapshots: list[ThreadStallSnapshot],
        *,
        system_quiescent: bool = True,
    ) -> StallReport | None:
        live = [s for s in snapshots if is_live(s.status)]
        # Reservation waits join the blocked set only under system quiescence:
        # an in-flight sibling execution can hold, then release, the resource.
        blocked = _INTERNALLY_BLOCKED if system_quiescent else _STALL_CANDIDATE_WAITS
        if (
            not live
            or not all(_is_blocked(s, blocked) for s in live)
            or not any(_is_candidate(s) for s in live)
        ):
            self._reset()
            return None

        signature = frozenset(
            (s.thread_id, s.status.value, s.waiting_on or "", s.following_peer_action)
            for s in live
        )
        if signature == self._prev_signature:
            self._stable_count += 1
        else:
            self._prev_signature = signature
            self._stable_count = 1
            self._reported = False

        if self._stable_count < self._required or self._reported:
            return None
        self._reported = True

        ordered = sorted(live, key=lambda s: s.thread_id)
        waits = tuple(
            f"{s.thread_id} [{s.status.value}] waiting on {s.waiting_on or '?'}"
            for s in ordered
        )
        return StallReport(
            thread_ids=tuple(s.thread_id for s in ordered),
            waits=waits,
        )

    def _reset(self) -> None:
        self._reported = False
        self._prev_signature = None
        self._stable_count = 0
