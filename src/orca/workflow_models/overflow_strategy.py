"""Overflow strategies for capacity-limited slots.

User-pluggable hook for what happens when a slot rejects an enqueue.
Built-in strategies cover the common cases (stash for handoff, reject loudly).
Users implement IOverflowStrategy (re-exported from labware_state) directly
to plug in custom routing.

The IOverflowStrategy/IWorkflowRef Protocols live in resource_models.labware_state
(not here) so LabwareSlot can type its overflow_strategy field without a
circular import. This module only contains the built-in strategies and the
selector.
"""
from typing import NoReturn

from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.capacity import (
    CapacityExceededError,
    OverflowAction,
    RecoverableCapacityExceededError,
)
from orca.resource_models.labware_state import (
    IOverflowStrategy,
    IWorkflowRef,
    LabwareSlot,
)
from orca.workflow_models.method import ExecutingMethod

__all__ = [
    "IOverflowStrategy",
    "IWorkflowRef",
    "SequentialStashStrategy",
    "RejectStrategy",
    "RecoverableRejectStrategy",
    "refuse_the_spent_candidate",
    "select_strategy",
]


def _emit_slot_event(workflow: IWorkflowRef, slot_key: str, status: str) -> None:
    event_name = f"SLOT.{slot_key}.{status}"
    context = WorkflowExecutionContext(
        execution_id=workflow.id, workflow_name=workflow.name,
    )
    workflow.event_bus.emit(event_name, context)


class SequentialStashStrategy:
    """Stash in slot.pending; drain at handoff when the active receiver ENDs.

    Matches existing SpawnNewOnFourthPlate-style chain semantics: one active
    receiver at a time, overflow batched sequentially behind it.
    """

    def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                    workflow: IWorkflowRef) -> None:
        slot.pending.append(method)
        _emit_slot_event(workflow, slot.slot_key, "OVERFLOW")


class RejectStrategy:
    """Raise CapacityExceededError. Used when overflow_action == REJECT."""

    def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                    workflow: IWorkflowRef) -> None:
        _emit_slot_event(workflow, slot.slot_key, "REJECTED")
        max_str = str(slot.policy.max_contributions) if slot.policy else "?"
        raise CapacityExceededError(
            f"Slot '{slot.slot_key}' at capacity (max={max_str}); "
            f"method '{method.name}' rejected per OverflowAction.REJECT"
        )


class RecoverableRejectStrategy:
    """Pause the contributor for operator decision instead of failing the workflow.

    Emits SLOT.{key}.AWAITING_DECISION (for UI/observers) and raises
    RecoverableCapacityExceededError. The action-loop's spawn-callback
    wrapper catches this subclass and routes through the thread's
    _pause_for_error path: the contributor's status becomes PAUSED, the
    workflow keeps running, and the operator picks one of:
    - RecoveryDecision.RETRY: re-attempt the spawn (succeeds if capacity
      freed in the interim, e.g. via slot.close() + fresh receiver).
    - RecoveryDecision.ABORT_THREAD: terminate the contributor.
    No action-level decision applies here: the spawn callback fires before
    action resolution, so there is no current action to recover. CONTINUE /
    ABORT_ACTION / ABORT_METHOD all re-pause the contributor with a warning.

    The rejected method is NOT stashed in slot.pending. The contributor
    holds its own work; on RETRY the spawn re-attempts and either lands or
    pauses again. To unblock a stuck slot, the operator can pair this with
    slot.close() (forces the active receiver to wrap up; a fresh receiver
    spawns on next contribution).
    """

    def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                    workflow: IWorkflowRef) -> None:
        _emit_slot_event(workflow, slot.slot_key, "AWAITING_DECISION")
        max_str = str(slot.policy.max_contributions) if slot.policy else "?"
        raise RecoverableCapacityExceededError(
            f"Slot '{slot.slot_key}' at capacity (max={max_str}); "
            f"method '{method.name}' awaiting operator decision per "
            f"OverflowAction.RECOVERABLE_REJECT"
        )


def select_strategy(slot: LabwareSlot) -> IOverflowStrategy:
    """Pick the strategy for a slot. Slot-attached custom takes precedence;
    then policy.overflow_action maps to a built-in; None policy falls back
    to SequentialStashStrategy.
    """
    if slot.overflow_strategy is not None:
        custom = slot.overflow_strategy
        if not isinstance(custom, IOverflowStrategy):
            raise TypeError(
                f"slot.overflow_strategy must implement IOverflowStrategy "
                f"(on_overflow method), got {type(custom).__name__}"
            )
        return custom
    if slot.policy is None:
        return SequentialStashStrategy()
    if slot.policy.overflow_action is OverflowAction.REJECT:
        return RejectStrategy()
    if slot.policy.overflow_action is OverflowAction.RECOVERABLE_REJECT:
        return RecoverableRejectStrategy()
    return SequentialStashStrategy()


def refuse_the_spent_candidate(
    slot: LabwareSlot, workflow: IWorkflowRef, reason: str,
) -> NoReturn:
    """Refuse a contribution because the labware the slot adopted is used up.

    Every strategy above overflows PAST an active receiver to a replacement.
    Here the labware was adopted rather than replaced, and a reuse-bound thread
    has no route in: a replacement would ask for a placement at a site the spent
    labware still occupies, with no thread owning it to take it off. So the
    contributor pauses and the operator is told both halves. RETRY re-runs the
    spawn, which succeeds once a labware with something in it is standing there
    and its contents are stated.
    """
    _emit_slot_event(workflow, slot.slot_key, "AWAITING_DECISION")
    raise RecoverableCapacityExceededError(reason)
