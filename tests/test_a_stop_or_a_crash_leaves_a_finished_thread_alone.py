"""Stop, crash and cancel handlers act on a thread that is still live and skip one that has finished."""

import logging

import pytest

from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.thread_at_status import FINISHED, STATUSES_TO_TRY, a_thread_at

STOPPING_OR_FINISHED = [LabwareThreadStatus.STOPPING, *FINISHED]


@pytest.mark.parametrize("status", STATUSES_TO_TRY, ids=lambda s: s.name)
def test_a_stop_request_moves_only_a_thread_that_is_neither_stopping_nor_finished(
    status: LabwareThreadStatus,
) -> None:
    thread = a_thread_at(status)

    thread.stop()

    expected = status if status in STOPPING_OR_FINISHED else LabwareThreadStatus.STOPPING
    assert thread.status is expected


@pytest.mark.parametrize("status", STATUSES_TO_TRY, ids=lambda s: s.name)
def test_a_crash_fails_only_a_thread_that_has_not_finished(
    status: LabwareThreadStatus, caplog: pytest.LogCaptureFixture,
) -> None:
    thread = a_thread_at(status)
    crash = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="orca"):
        thread._fire_thread_failed(crash)

    if status in FINISHED:
        assert thread.status is status
        assert thread.last_error is None
        assert caplog.records == []
    else:
        assert thread.status is LabwareThreadStatus.FAILED
        assert thread.last_error is crash


@pytest.mark.parametrize("status", STATUSES_TO_TRY, ids=lambda s: s.name)
def test_a_cancel_stops_only_a_thread_that_has_not_finished(
    status: LabwareThreadStatus, caplog: pytest.LogCaptureFixture,
) -> None:
    thread = a_thread_at(status)

    with caplog.at_level(logging.ERROR, logger="orca"):
        thread._fire_thread_stopped_on_cancel()

    expected = status if status in FINISHED else LabwareThreadStatus.STOPPED
    assert thread.status is expected
    assert caplog.records == []
