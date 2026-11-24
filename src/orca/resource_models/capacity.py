"""Capacity policy data types for slot-level convergence overflow.

Slot-level capacity declaration. Pure data: no behavior, no protocol references
to workflow types. The strategy that ACTS on overflow lives in
workflow_models/overflow_strategy.py where it can reference LabwareSlot,
ExecutingMethod, and ExecutingWorkflow without circular imports.

Reuse of the active receiver requires BOTH gates to pass: the spawn callback
tests ``can_continue and slot.has_room()``. It is a conjunction, not a
precedence -- either one saying no sends the contribution to the overflow
strategy, so a permissive can_continue does NOT override max_contributions
(nor the reverse).

Under-fill (declared N, only M<N arrive) strands no receiver, whichever loop it
uses. The slot closes when its contribution window is over (LabwareSlot.close,
driven by ExecutingWorkflow._evaluate_slot_closures). A
``while ctx.has_more_work()`` loop reads that directly and exits. A hand-rolled
``for i in range(N)`` loop does not hang either: once the slot is closed AND
its queue is drained, orca.join() yields no method (MethodTemplate.schedule
returns without yielding), so the remaining iterations are no-ops and the loop
runs out. A join can still serve work queued behind the close sentinel first.

Prefer has_more_work() regardless -- it ends at the real window boundary rather
than at a hardcoded guess, so it neither burns no-op iterations on under-fill
nor caps a receiver below a window that is still open.
"""
from dataclasses import dataclass
from enum import Enum


class OverflowAction(str, Enum):
    """What the next contributor gets once a receiver is full or used up.

    NEW: a new receiver, started as soon as the full one stops taking contributions; the two overlap.
    REJECT: while the full receiver still runs, the contributor pauses for the operator.
    """

    NEW = "NEW"
    REJECT = "REJECT"


@dataclass(frozen=True)
class CapacityPolicy:
    """Per-slot capacity declaration attached via `wf.thread(thread, capacity=...)`.

    BOTH GATES MUST PASS to reuse the active receiver: the spawn callback tests
    ``can_continue and slot.has_room()``. So max_contributions=N is an upper
    bound, not a guarantee -- a labware reporting can_continue()=False (e.g.
    TipRackInstance with no tips_present in the ops history, or a registered
    can_continue_fn) overflows to a fresh receiver with far fewer than N
    enqueued. Equally, has_room()=False overflows even when can_continue is True.

    Beware: can_continue reads CURRENT physical state, so it cannot account for
    a contribution already bound to this receiver whose action has not run yet.
    """

    max_contributions: int
    overflow_action: OverflowAction = OverflowAction.NEW

    def __post_init__(self) -> None:
        if self.max_contributions <= 0:
            raise ValueError(
                f"max_contributions must be > 0, got {self.max_contributions}"
            )


class CapacityExceededError(RuntimeError):
    """A contribution reached a REJECT slot that is full. The contributor pauses for the operator.

    RETRY runs the spawn again; it succeeds once capacity frees. ABORT_THREAD ends only the contributor.
    The action-level verbs do not apply: the spawn runs before the action is resolved.
    """
