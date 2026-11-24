"""The stall detector reads a thread as live unless it has finished (completed, stopped, aborted or failed)."""

import pytest

from orca.runtime.stall_detector import is_in_flight, is_live
from orca.workflow_models.status_enums import LabwareThreadStatus

FINISHED = frozenset({
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.FAILED,
})


@pytest.mark.parametrize("status", list(LabwareThreadStatus), ids=lambda s: s.name)
def test_a_thread_is_live_unless_it_has_finished(status: LabwareThreadStatus) -> None:
    assert is_live(status) == (status not in FINISHED)


@pytest.mark.parametrize("status", [s for s in LabwareThreadStatus if s in FINISHED], ids=lambda s: s.name)
def test_a_finished_thread_is_never_in_flight(status: LabwareThreadStatus) -> None:
    assert not is_in_flight(status)
