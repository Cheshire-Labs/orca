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
    """Selects which built-in overflow strategy applies when capacity is reached."""

    NEW = "NEW"
    REJECT = "REJECT"
    RECOVERABLE_REJECT = "RECOVERABLE_REJECT"


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
    """Raised by RejectStrategy when a REJECT-policy slot is full.

    User-visible behavior:
    - Raised SYNCHRONOUSLY from the spawn callback running inside a thread's
      action loop (_auto_spawn_for_action -> callback -> strategy.on_overflow).
    - Propagates up through _run_method_loop to asyncio.gather in
      ExecutingWorkflow.start. The originating thread's status becomes
      ERRORED via the existing error-handling path.
    - The workflow's overall status becomes FAILED, same outcome as any
      uncaught exception in a thread today.
    - Exposed via orca.CapacityExceededError so user code wrapping
      runtime.run() can catch it explicitly.

    For the recoverable variant (operator decision instead of workflow
    failure), see RecoverableCapacityExceededError below.
    """

    pass


class RecoverableCapacityExceededError(CapacityExceededError):
    """Raised by RecoverableRejectStrategy to pause the contributor for operator
    decision instead of failing the workflow.

    Distinguished from the base CapacityExceededError by the action-loop's
    spawn-callback wrapper, which catches this variant and routes through
    the thread's _pause_for_error path. The contributor's status becomes
    PAUSED; operator decisions accepted at this pause site:
    - RecoveryDecision.RETRY: re-runs the spawn callback. Succeeds if state
      changed in the interim (e.g., the active receiver completed, slot.close()
      was called and a fresh receiver is ready, capacity was raised).
    - RecoveryDecision.ABORT_THREAD: terminates the contributor thread.

    Every action-level decision is unsupported here: the spawn callback fires
    before action resolution, so ExecutingMethod has no `_current_action` to
    recover. CONTINUE / ABORT_ACTION / ABORT_METHOD are warned about and the
    contributor re-pauses for a decision that applies. CONTINUE additionally
    never reaches this loop for a thread parked here alone, because
    `resume_with_decision` refuses it without a bound action; it arrives only
    when the thread's SHARED group is separately action-paused. Future
    enhancement: skip-spawn semantics that advance the method's lane without
    invoking handle_recovery.

    IS-A CapacityExceededError so any code catching the base class still
    observes the recoverable variant (defensive backstop if the recovery
    wrapper is ever bypassed).
    """

    pass
