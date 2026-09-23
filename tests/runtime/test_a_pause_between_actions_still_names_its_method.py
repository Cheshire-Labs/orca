"""`current_method` and `current_action` on a thread snapshot go null independently.

An operator surface picks a recovery verb from what the thread was trying to do, and
it reads that from `current_method.current_action`. A thread inside a method that has
not resolved its next action must still report the method, or a pause between actions
reads on every surface as a thread that was doing nothing.
"""

from unittest.mock import MagicMock

from orca.runtime.status_builders import _build_thread_snapshot
from orca.workflow_models.status_enums import LabwareThreadStatus, MethodStatus


def _paused_thread(*, assigned_method: MagicMock | None) -> MagicMock:
    thread = MagicMock()
    thread.id = "t1"
    thread.name = "plate_1-abcd"
    thread.status = LabwareThreadStatus.PAUSED
    thread.current_location.name = "shaker_1"
    thread.assigned_method = assigned_method
    thread.completed_methods = []
    thread.last_error = RuntimeError("shaker_1 is not connected")
    thread.labware_template = None
    thread.paused_device_command = None
    thread.pause_message = "thread step failed: action resolution"
    return thread


def _method_with_no_resolved_action() -> MagicMock:
    method = MagicMock()
    method.id = "m1"
    method.name = "run_assay_step"
    method.status = MethodStatus.IN_PROGRESS
    method.current_action = None
    method.completed_actions = []
    return method


def test_a_method_with_no_resolved_action_is_still_reported() -> None:
    snap = _build_thread_snapshot(_paused_thread(assigned_method=_method_with_no_resolved_action()))

    assert snap.current_method is not None
    assert snap.current_method.name == "run_assay_step"
    assert snap.current_method.current_action is None


def test_current_method_is_null_only_when_no_method_is_assigned() -> None:
    """Between methods, and on the move to the thread's end position, there is none."""
    snap = _build_thread_snapshot(_paused_thread(assigned_method=None))

    assert snap.current_method is None
    assert snap.pause_message == "thread step failed: action resolution"
