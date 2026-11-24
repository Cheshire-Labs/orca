"""Overflow strategies for capacity-limited slots.

User-pluggable hook for what happens when a slot rejects an enqueue.
Built-in strategies cover the common cases (stash for handoff, pause the contributor).
Users implement IOverflowStrategy (re-exported from labware_state) directly
to plug in custom routing.

The IOverflowStrategy/IWorkflowRef Protocols live in workflow_models.labware_state
(not here) so LabwareSlot can type its overflow_strategy field without a
circular import. This module only contains the built-in strategies and the
selector.
"""
from typing import NoReturn

from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.capacity import CapacityExceededError, OverflowAction
from orca.workflow_models.labware_state import (
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
    "refuse_the_candidate",
    "select_strategy",
]


def _emit_slot_event(workflow: IWorkflowRef, slot_key: str, status: str) -> None:
    event_name = f"SLOT.{slot_key}.{status}"
    context = WorkflowExecutionContext(
        execution_id=workflow.id, workflow_name=workflow.name,
    )
    workflow.event_bus.emit(event_name, context)


class SequentialStashStrategy:
    """Stash in slot.pending until the handoff, which routes the stash to a new receiver.

    Under OverflowAction.NEW the handoff comes as soon as the receiver stops taking contributions.
    Attached explicitly, the handoff waits for the receiver to end: one receiver at a time.
    """

    def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                    workflow: IWorkflowRef) -> None:
        slot.pending.append(method)
        _emit_slot_event(workflow, slot.slot_key, "OVERFLOW")


class RejectStrategy:
    """Pause the contributor for the operator. Used when overflow_action == REJECT.

    The rejected method is not stashed in slot.pending: the contributor keeps its own work, and RETRY
    runs the spawn again.
    """

    def on_overflow(self, slot: LabwareSlot, method: ExecutingMethod,
                    workflow: IWorkflowRef) -> None:
        _emit_slot_event(workflow, slot.slot_key, "REJECTED")
        max_str = str(slot.policy.max_contributions) if slot.policy else "?"
        raise CapacityExceededError(
            f"Slot '{slot.slot_key}' at capacity (max={max_str}); "
            f"method '{method.name}' paused per OverflowAction.REJECT"
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
    return SequentialStashStrategy()


def refuse_the_candidate(
    slot: LabwareSlot, workflow: IWorkflowRef, reason: str,
) -> NoReturn:
    """Refuse a contribution because the labware standing at a reuse site cannot serve it.

    It is used up or the wrong type. Every strategy above overflows PAST an
    active receiver to a replacement, but a reuse-bound thread has no route in:
    a replacement would ask for a placement at a site that labware still
    occupies, with no thread owning it to take it off. So the contributor pauses
    and the operator is told both halves. RETRY re-runs the spawn.
    """
    refuse_the_spawn(workflow, slot.slot_key, reason)


def refuse_the_spawn(workflow: IWorkflowRef, slot_key: str, reason: str) -> NoReturn:
    """Pause the contributor with ``reason``; RETRY runs the spawn again."""
    _emit_slot_event(workflow, slot_key, "AWAITING_DECISION")
    raise CapacityExceededError(reason)
