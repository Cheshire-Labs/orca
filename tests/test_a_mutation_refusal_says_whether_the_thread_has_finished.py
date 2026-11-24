"""A refused mutation says whether the thread is finished or only not paused, for every thread status."""

import pytest

from orca.system.mutation.errors import ThreadNotPausedError
from orca.workflow_models.status_enums import LabwareThreadStatus

FINISHED = frozenset({
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.FAILED,
})


@pytest.mark.parametrize("status", list(LabwareThreadStatus), ids=lambda s: s.name)
def test_the_refusal_marks_only_finished_threads_as_terminal(status: LabwareThreadStatus) -> None:
    error = ThreadNotPausedError(thread_name="plate_1", current_status=status.name)

    assert error.terminal == (status in FINISHED)

