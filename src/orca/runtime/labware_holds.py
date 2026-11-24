"""What a thread's status means for an operator changing its labware.

The operator verbs ask here and carry the answer out. A status that cannot be read counts as
holding the labware. The rows are pinned in ``tests/test_operator_changes_to_held_labware.py``.
"""

from orca.workflow_models.status_enums import LabwareThreadStatus

_USING_ITS_POSITION: dict[LabwareThreadStatus, str] = {
    LabwareThreadStatus.MOVING: "is being moved",
    LabwareThreadStatus.EXECUTING_ACTION: "is in an action",
}


def holds_labware(status: LabwareThreadStatus | None) -> bool:
    """A thread parked at AWAITING_MANUAL_REMOVE is waiting for exactly this discharge."""
    if status is None:
        return True
    return not status.is_terminal() and status is not LabwareThreadStatus.AWAITING_MANUAL_REMOVE


def refusal_to_change_position(
    status: LabwareThreadStatus | None, labware_name: str, thread_name: str,
) -> str | None:
    doing = _USING_ITS_POSITION.get(status) if status is not None else None
    if doing is None:
        return None
    return (
        f"{labware_name} {doing} by thread {thread_name}. "
        f"Wait for that to finish, or pause the run, then record its position."
    )
