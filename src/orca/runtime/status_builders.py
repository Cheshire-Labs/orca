"""Turn live engine objects into the snapshots the wire carries.

Apart from `status_models` because these take `ExecutingLabwareThread` and
friends in their signatures, so importing them means importing the engine.
The models themselves have to stay reachable without it.
"""

from orca.runtime.status_models import (
    ActionSnapshot,
    MethodSnapshot,
    ThreadSnapshot,
)
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.status_enums import LabwareThreadStatus


def _build_action_snapshot(action: ExecutableLocationAction) -> ActionSnapshot:
    return ActionSnapshot(
        id=action.action.id,
        command=action.action.command,
        status=action.status.name,
        position_id=action.action.location.position_id,
        resource_name=action.action.device.name,
        description=action.action.description,
    )


def _build_method_snapshot(method: ExecutingMethod) -> MethodSnapshot:
    current_action = method.current_action
    action_snapshot: ActionSnapshot | None = None
    if current_action is not None:
        action_snapshot = _build_action_snapshot(current_action)

    return MethodSnapshot(
        id=method.id,
        name=method.name,
        status=method.status.name,
        current_action=action_snapshot,
        completed_action_count=len(method.completed_actions),
    )


def derive_waiting_for(thread: ExecutingLabwareThread) -> str | None:
    """Return the specific subject of this thread's wait, or None.

    Two kinds of wait answer here. An ``AWAITING_*`` status names its own
    subject. A device or transporter lock wait has no status of its
    own, because it keeps whatever the thread already had -- ``MOVING`` for an
    arm queued to reach into a busy handler -- so it is asked first, before a
    status that would report nothing.

    Reads only; never mutates thread or action state.
    """
    blocked_on_lock = thread.blocked_on_lock
    if blocked_on_lock is not None:
        return blocked_on_lock

    status = thread.status

    if status == LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY:
        move_action = thread.move_action
        if move_action is not None:
            return move_action.target.name
        return None

    if status == LabwareThreadStatus.AWAITING_MOVE_RESERVATION:
        # End-of-thread moves (line 1204 in executing_labware_thread.py) flip
        # status before assigning _move_action; the dispatch loop nulls
        # _assigned_action at end of each action, so the assigned_method check
        # must come BEFORE the assigned_action fallback to keep the helper
        # robust against any future reordering of the null-out sites.
        move_action = thread.move_action
        if move_action is not None:
            return move_action.target.name
        if thread.assigned_method is None:
            # Comma-joined like every other multi-subject waiting_for
            # (missing co-labware, contended action candidates).
            return ", ".join(loc.name for loc in thread.end_locations)
        assigned_action = thread.assigned_action
        if assigned_action is not None:
            return assigned_action.action.location.name
        return None

    if status == LabwareThreadStatus.AWAITING_CO_THREADS:
        assigned_action = thread.assigned_action
        if assigned_action is None:
            return None
        # Unlike peek_missing_input_labware, this also names slots no thread
        # has assigned at all -- the case with no subject to report before.
        missing = assigned_action.missing_input_report()
        if not missing:
            return None
        return ", ".join(missing)

    if status == LabwareThreadStatus.RESOLVING_ACTION_LOCATION:
        method = thread.assigned_method
        if method is not None:
            return method.name
        return None

    if status == LabwareThreadStatus.AWAITING_ACTION_RESERVATION:
        # Round 5 S1-A: the resource-pool resolver's retry loop populates
        # ``thread._pending_reservation_candidates`` with the locations
        # it is racing against. Surface them as a comma-joined list so
        # the operator sees what is contended ("shaker_1, shaker_2")
        # instead of a silent stall in RESOLVING_ACTION_LOCATION.
        candidates = thread.pending_reservation_candidates
        if candidates:
            return ", ".join(sorted(candidates))
        return None

    if status == LabwareThreadStatus.AWAITING_MANUAL_PLACE:
        # LIVE-mode ManualPlaceSpawn polls `start_location.labware`
        # waiting for `labware_register` to write the operator's
        # instance. The wait's subject is the start_location name.
        return thread.start_location.name

    if status == LabwareThreadStatus.AWAITING_MANUAL_REMOVE:
        # ManualRemoveSpawn polls the slot the labware actually reached,
        # not a declared end alternative.
        return thread.current_location.name

    return None


def _build_thread_snapshot(thread: ExecutingLabwareThread) -> ThreadSnapshot:
    current_method = thread.assigned_method
    method_snapshot: MethodSnapshot | None = None
    if current_method is not None:
        method_snapshot = _build_method_snapshot(current_method)

    last_error_str: str | None = None
    if thread.last_error is not None:
        last_error_str = str(thread.last_error)

    pause_reason: str | None = None
    if thread.status == LabwareThreadStatus.PAUSED:
        pause_reason = "error" if thread.last_error is not None else thread.pause_reason_hint

    labware_template = thread.labware_template
    return ThreadSnapshot(
        id=thread.id,
        name=thread.name,
        status=thread.status.name,
        current_location=thread.current_location.name,
        current_method=method_snapshot,
        completed_method_count=len(thread.completed_methods),
        last_error=last_error_str,
        pause_reason=pause_reason,
        completed_methods=tuple(m.name for m in thread.completed_methods),
        labware_template_name=labware_template.name if labware_template is not None else None,
        labware_id=thread.labware.id,
        labware_name=thread.labware.name,
        paused_device_command=thread.paused_device_command,
        pause_message=thread.pause_message,
        pause_site=thread.pause_site.value if thread.pause_site is not None else None,
        honoured_decisions=tuple(d.value for d in thread.honoured_decisions),
        waiting_for=derive_waiting_for(thread),
    )
