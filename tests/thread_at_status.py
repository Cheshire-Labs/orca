"""Builds a real thread and walks its state machine to a chosen status."""

from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.thread_builder import build_executing_thread

STATUSES_TO_TRY = [
    LabwareThreadStatus.CREATED,
    LabwareThreadStatus.PAUSED,
    LabwareThreadStatus.STOPPING,
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.FAILED,
]

FINISHED = [
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.FAILED,
]

_EVENTS_TO_REACH: dict[LabwareThreadStatus, list[ThreadEvent]] = {
    LabwareThreadStatus.CREATED: [],
    LabwareThreadStatus.PAUSED: [ThreadEvent.PAUSE_REQUESTED],
    LabwareThreadStatus.STOPPING: [ThreadEvent.STOP_REQUESTED],
    LabwareThreadStatus.COMPLETED: [ThreadEvent.THREAD_COMPLETED],
    LabwareThreadStatus.ABORTED: [ThreadEvent.ABORT_THREAD],
    LabwareThreadStatus.STOPPED: [ThreadEvent.STOP_REQUESTED, ThreadEvent.STOP_COMPLETE],
    LabwareThreadStatus.FAILED: [ThreadEvent.THREAD_FAILED],
}


def a_thread_at(status: LabwareThreadStatus) -> ExecutingLabwareThread:
    thread = build_executing_thread()
    thread.publish_initial_status()
    for event in _EVENTS_TO_REACH[status]:
        thread._fire(event)
    assert thread.status is status
    return thread
