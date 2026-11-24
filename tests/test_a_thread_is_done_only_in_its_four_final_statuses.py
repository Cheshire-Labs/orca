"""A thread signals completion and reports completed only once it is COMPLETED, ABORTED, STOPPED or FAILED."""

import pytest

from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.thread_at_status import FINISHED, STATUSES_TO_TRY, a_thread_at


@pytest.mark.parametrize("status", STATUSES_TO_TRY, ids=lambda s: s.name)
def test_the_completed_event_is_set_only_when_the_thread_reaches_a_final_status(
    status: LabwareThreadStatus,
) -> None:
    thread = a_thread_at(status)

    assert thread.completed.is_set() == (status in FINISHED)


@pytest.mark.parametrize("status", STATUSES_TO_TRY, ids=lambda s: s.name)
def test_has_completed_is_true_only_in_a_final_status(status: LabwareThreadStatus) -> None:
    thread = a_thread_at(status)

    assert thread.has_completed() == (status in FINISHED)
