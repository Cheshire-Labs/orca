"""A thread's snapshot says its location is unknown only once the thread has ended."""

from unittest.mock import MagicMock, PropertyMock

import pytest

from orca.runtime.status_builders import _build_thread_snapshot
from orca.workflow_models.status_enums import LabwareThreadStatus


def _thread_with_no_tracked_position(status: LabwareThreadStatus) -> MagicMock:
    thread = MagicMock()
    thread.id = "t1"
    thread.name = "plate_1-abcd"
    thread.status = status
    thread.start_labware_never_arrived = False
    type(thread).current_location = PropertyMock(side_effect=KeyError("plate_1-abcd has no tracked position"))
    thread.assigned_method = None
    thread.assigned_action = None
    thread.completed_methods = []
    thread.last_error = None
    thread.labware_template = None
    thread.pause_reason_hint = None
    thread.paused_device_command = None
    thread.pause_message = None
    thread.pause_site = None
    thread.honoured_decisions = ()
    thread.blocked_on_lock = None
    return thread


@pytest.mark.parametrize("status", [
    LabwareThreadStatus.COMPLETED, LabwareThreadStatus.FAILED, LabwareThreadStatus.ABORTED,
])
def test_an_ended_thread_whose_labware_has_no_tracked_position_reports_no_location(status: LabwareThreadStatus) -> None:
    assert status.is_terminal()

    assert _build_thread_snapshot(_thread_with_no_tracked_position(status)).current_location is None


@pytest.mark.parametrize("status", [LabwareThreadStatus.MOVING, LabwareThreadStatus.PAUSED])
def test_a_thread_still_running_with_no_tracked_position_is_a_fault(status: LabwareThreadStatus) -> None:
    with pytest.raises(KeyError, match="no tracked position"):
        _build_thread_snapshot(_thread_with_no_tracked_position(status))
