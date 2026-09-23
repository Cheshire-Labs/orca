from dataclasses import dataclass
from enum import Enum

class ActionStatus(str, Enum):
    """Lifecycle phase of a single action.

    Inheriting from ``str`` (with explicit string values that match each
    member's ``.name``) makes the JSON wire shape intrinsic: default
    ``model_dump(mode="json")`` and ``json.dumps`` of an ``ActionStatus``
    value emit the canonical name string (``"COMPLETED"``, ``"PICKING"``,
    ...) rather than an opaque ``auto()`` int. Existing code paths that
    used ``.name`` keep producing the same wire form because ``.value``
    now equals ``.name`` by construction.
    """
    CREATED = "CREATED"
    RESOLVED = "RESOLVED"
    AWAITING_LOCATION_RESERVATION = "AWAITING_LOCATION_RESERVATION"
    AWAITING_CO_THREADS = "AWAITING_CO_THREADS"
    EXECUTING_ACTION = "EXECUTING_ACTION"
    AWAITING_MOVE_RESERVATION = "AWAITING_MOVE_RESERVATION"
    PREPARING_TO_MOVE = "PREPARING_TO_MOVE"
    PICKING = "PICKING"
    PLACING = "PLACING"
    COMPLETED = "COMPLETED"
    ERRORED = "ERRORED"
    SKIPPED = "SKIPPED"
    ABORTED = "ABORTED"

class MethodStatus(str, Enum):
    """Lifecycle phase of one executing method.

    See ``ActionStatus`` for the rationale behind the explicit string
    values (intrinsic wire shape, no reliance on per-site
    ``field_serializer`` to emit ``.name``).
    """
    CREATED = "CREATED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    PARTIAL_COMPLETE = "PARTIAL_COMPLETE"

class LabwareThreadStatus(str, Enum):
    """Lifecycle phase of one labware thread.

    See ``ActionStatus`` for the rationale behind the explicit string
    values (intrinsic wire shape, no reliance on per-site
    ``field_serializer`` to emit ``.name``).
    """
    CREATED = "CREATED"
    AWAITING_MANUAL_PLACE = "AWAITING_MANUAL_PLACE"
    RESOLVING_ACTION_LOCATION = "RESOLVING_ACTION_LOCATION"
    AWAITING_ACTION_RESERVATION = "AWAITING_ACTION_RESERVATION"
    ACTION_LOCATION_RESOLVED = "ACTION_LOCATION_RESOLVED"
    AWAITING_MOVE_RESERVATION = "AWAITING_MOVE_RESERVATION"
    AWAITING_MOVE_TARGET_AVAILABILITY = "AWAITING_MOVE_TARGET_AVAILABILITY"
    MOVING = "MOVING"
    AWAITING_CO_THREADS = "AWAITING_CO_THREADS"
    EXECUTING_ACTION = "EXECUTING_ACTION"
    AWAITING_EVENT = "AWAITING_EVENT"
    AWAITING_MANUAL_REMOVE = "AWAITING_MANUAL_REMOVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    """Terminal, crash-only: the thread's task died on an unhandled error.
    Distinct from ABORTED (operator decision) and STOPPED (operator stop)."""

class WorkflowStatus(str, Enum):
    """Lifecycle phase of one workflow run.

    See ``ActionStatus`` for the rationale behind the explicit string
    values (intrinsic wire shape, no reliance on per-site
    ``field_serializer`` to emit ``.name``).
    """
    CREATED = "CREATED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ERRORED = "ERRORED"

class FailurePolicy(str, Enum):
    """How an action error is handled.

    Inheriting from ``str`` (with explicit string values that match each
    member's ``.name``) makes the JSON wire shape intrinsic: Pydantic v2's
    default ``model_dump(mode="json")`` emits ``.value`` -- now identical
    to ``.name`` -- so a DTO that types a field as ``FailurePolicy`` and
    forgets a custom ``field_serializer`` still ships the string form
    (``"PAUSE"`` / ``"ABORT"``) rather than an opaque ``auto()`` int.
    """
    ABORT = "ABORT"
    PAUSE = "PAUSE"

class PauseSite(str, Enum):
    """WHERE a paused thread stopped, which decides what it can be told next.

    A thread pauses at several places and they honour different
    ``RecoveryDecision`` verbs. The wire carried only the error text, so a
    client could not tell them apart and had to send a decision to find out --
    at a failed move the wrong one fails the whole execution. The engine knows
    this for certain, so it says it.

    What each site honours is ``HONOURED_DECISIONS`` below, which is the only
    thing that enforces it. An earlier note here argued against writing it as
    data because a second copy would drift; that was right about a copy and
    wrong about this. Nothing else refused a verb, so the enforcement was a
    fallthrough nobody chose, and it fails the thread.
    """

    ACTION_BODY = "ACTION_BODY"
    """An action's Python raised. The site the docs have always described."""

    DEVICE_OP = "DEVICE_OP"
    """Suspended inside one device call. ``paused_device_command`` names it."""

    MOVE = "MOVE"
    """A transporter move raised. CONTINUE additionally needs the ledger to put
    the labware at the target; the operator records that themselves."""

    THREAD_STEP = "THREAD_STEP"
    """The thread body itself raised, outside any action."""

    WAIT_EVENT_TIMEOUT = "WAIT_EVENT_TIMEOUT"
    """A ``wait_event`` ran out of time."""

    ACTION_RESOLUTION = "ACTION_RESOLUTION"
    """Failed before any action was bound, so there is no action to act on."""

    MOVE_RESOLUTION = "MOVE_RESOLUTION"
    """No route to plan, usually because someone said the labware is somewhere
    nothing serving this move can reach."""

    DEADLOCK = "DEADLOCK"
    """The reservation graph could not be resolved."""

    SPAWN_CAPACITY = "SPAWN_CAPACITY"
    """An auto-spawn hit a capacity limit before an action existed."""


class RecoveryDecision(str, Enum):
    """Operator decision when recovering an error-paused thread.

    See ``ActionStatus`` for the rationale behind the explicit string
    values (intrinsic wire shape, no reliance on per-site
    ``field_serializer`` to emit ``.name``).

    ``RETRY`` is action-level: re-run the whole action body from the top
    (re-resolving variables). ``RETRY_OP`` is operation-level: re-run ONLY the
    failed device call, with the action body still suspended at its await.

    ``CONTINUE`` and ``ABORT_ACTION`` both advance to the next action, and they
    mean different things. ``ABORT_ACTION`` discards the action as work that
    never happened. ``CONTINUE`` is the operator saying the situation is dealt
    with -- the call really succeeded, or they fixed it by hand -- so the run
    carries on and the ledger records the action as operator-confirmed rather
    than executed. Neither one promises the action's work got done.

    ``CONTINUE`` says the same thing at a failed move: the operator carried the
    labware to the target themselves. It is refused there until the ledger says
    the labware IS at the target, since the run has to resolve the next action
    from somewhere.
    """
    RETRY = "RETRY"
    RETRY_OP = "RETRY_OP"
    CONTINUE = "CONTINUE"
    ABORT_ACTION = "ABORT_ACTION"
    ABORT_METHOD = "ABORT_METHOD"
    ABORT_THREAD = "ABORT_THREAD"


ESCALATION_ORDER: tuple[RecoveryDecision, ...] = tuple(RecoveryDecision)
"""Cheapest recovery first, as the enum declares them. Sorting these any
other way puts three aborts ahead of RETRY, which is the one an operator
usually wants."""


@dataclass(frozen=True)
class SiteRecovery:
    """What a pause site honours, and what to tell an operator who misses."""

    honours: frozenset[RecoveryDecision]
    because: str
    """One sentence naming what this site lacks, shown on a refusal."""


_ACTION_LEVEL = frozenset({
    RecoveryDecision.RETRY,
    RecoveryDecision.CONTINUE,
    RecoveryDecision.ABORT_ACTION,
    RecoveryDecision.ABORT_METHOD,
    RecoveryDecision.ABORT_THREAD,
})

_NOTHING_BOUND = frozenset({
    RecoveryDecision.RETRY,
    RecoveryDecision.ABORT_THREAD,
})

_NOTHING_BOUND_BECAUSE = (
    "nothing is bound here, so there is no action to carry past or discard; "
    "the thread can only try again or end"
)

_NOTHING_BOUND_BUT_ABORTABLE = _NOTHING_BOUND | {
    RecoveryDecision.ABORT_ACTION,
    RecoveryDecision.ABORT_METHOD,
}

_ENDS_THE_RENDEZVOUS = (
    "there is no bound action, so the narrower aborts cannot discard one and "
    "end the thread instead -- which is deliberate: the alternative was "
    "leaving the owner and its contributors parked at PAUSED forever"
)

HONOURED_DECISIONS: dict[PauseSite, SiteRecovery] = {
    PauseSite.ACTION_BODY: SiteRecovery(_ACTION_LEVEL, "no device call is suspended"),
    PauseSite.DEVICE_OP: SiteRecovery(
        _ACTION_LEVEL | {RecoveryDecision.RETRY_OP}, "every decision applies here",
    ),
    PauseSite.MOVE: SiteRecovery(
        frozenset({
            RecoveryDecision.RETRY,
            RecoveryDecision.CONTINUE,
            RecoveryDecision.ABORT_THREAD,
        }),
        "a move is not a method action, so the action-level aborts have nothing "
        "to discard, and the labware would be left where the next action does "
        "not expect it",
    ),
    PauseSite.THREAD_STEP: SiteRecovery(_NOTHING_BOUND, _NOTHING_BOUND_BECAUSE),
    PauseSite.WAIT_EVENT_TIMEOUT: SiteRecovery(_NOTHING_BOUND, _NOTHING_BOUND_BECAUSE),
    PauseSite.ACTION_RESOLUTION: SiteRecovery(
        _NOTHING_BOUND_BUT_ABORTABLE, _ENDS_THE_RENDEZVOUS,
    ),
    PauseSite.MOVE_RESOLUTION: SiteRecovery(
        _NOTHING_BOUND,
        "no route could be planned, so no move exists to call finished; correct "
        "the position and try again",
    ),
    PauseSite.DEADLOCK: SiteRecovery(
        _NOTHING_BOUND_BUT_ABORTABLE, _ENDS_THE_RENDEZVOUS,
    ),
    PauseSite.SPAWN_CAPACITY: SiteRecovery(
        _NOTHING_BOUND,
        "the spawn runs before an action is resolved, so there is no action to "
        "carry past or discard; free capacity and try again",
    ),
}
"""What each pause site honours. The one statement of the rule.

A verb this refuses never reaches the thread: the operator is told on the call
and the thread stays paused, so they can pick again. Before this, seven of the
nine sites refused nothing and an unhonoured verb fell through to code that
failed the thread, or -- at resolution and deadlock -- silently acted as
ABORT_THREAD instead.

It does not say what a site DOES with a verb it honours. That stays at the site,
because it is different work in each one. The drift this leaves is a verb
permitted here that no site code acts on, and
``tests/test_a_pause_site_honours_what_it_says.py`` is what catches it.
"""
