"""Submission carries the resolved ``run_mode`` so operators can confirm
"this is PURE_SIM, not LIVE" via MCP without re-deriving it.

The run mode is resolved at submit time (per the C1 precedence:
submit_override > topology per-device sim_override > deployment base).
Once resolved, it stamps the Submission and surfaces unchanged through
``SubmissionSnapshot`` and ``SubmissionDTO`` to the operator.
"""

import dataclasses
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from orca.daemon.schemas import SubmissionDTO
from orca.runtime.facades.submissions import _snapshot
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import SubmissionSnapshot
from orca.runtime.submission import BatchMode, Submission, SubmissionStatus


def _make_submission(
    run_mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM,
) -> Submission:
    return Submission(
        id="sub-1",
        execution_id="exec-1",
        workflow_name="hello",
        groups=(),
        variables={},
        batch_mode=BatchMode.STANDALONE,
        submitted_at=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        run_mode=run_mode,
        operator_id=None,
        deployment_profile=None,
        status=SubmissionStatus.ACCEPTED,
    )


def test_facade_snapshot_carries_submission_run_mode() -> None:
    """The submissions facade stamps the operator-facing snapshot from the
    stored ``Submission.run_mode``. This is the real downstream consumer:
    MCP/REST read ``SubmissionSnapshot.run_mode`` and never re-derive it. A
    regression that drops or hardcodes the field in ``_snapshot`` fails here.
    """
    sub = _make_submission(WorkflowRunMode.LIVE)
    snap = _snapshot(sub)
    assert snap.run_mode is WorkflowRunMode.LIVE

    other = _make_submission(WorkflowRunMode.DEVICE_SIM)
    assert _snapshot(other).run_mode is WorkflowRunMode.DEVICE_SIM


def test_submission_dto_round_trips_run_mode() -> None:
    """SubmissionDTO is the wire shape MCP and REST serialize. The
    resolved run_mode must round-trip through model_validate/dump."""
    snap = SubmissionSnapshot(
        id="sub-1",
        execution_id="exec-1",
        workflow_name="hello",
        group_count=0,
        status=SubmissionStatus.ACCEPTED,
        batch_mode=BatchMode.STANDALONE,
        submitted_at="2026-05-15T12:00:00+00:00",
        run_mode=WorkflowRunMode.LIVE,
        operator_id=None,
        deployment_profile=None,
    )
    payload = SubmissionDTO.model_validate(dataclasses.asdict(snap))
    assert payload.run_mode is WorkflowRunMode.LIVE
    dumped = payload.model_dump(mode="json")
    assert dumped["run_mode"] == "LIVE"


def test_submission_dto_rejects_missing_run_mode() -> None:
    """``run_mode`` is required on SubmissionDTO; the runtime always
    stamps it via SubmissionSnapshot. A wire payload missing the
    field must fail loudly at validation so consumers cannot silently
    receive a None where a WorkflowRunMode is expected.

    Uses ``model_validate`` (dict-based) rather than the constructor
    so pyright sees the test as expressing "missing field on the
    wire," not "I forgot to pass a required argument."
    """
    with pytest.raises(ValidationError):
        SubmissionDTO.model_validate({
            "id": "sub-1",
            "execution_id": "exec-1",
            "workflow_name": "hello",
            "group_count": 0,
            "status": SubmissionStatus.ACCEPTED.value,
            "batch_mode": BatchMode.STANDALONE.value,
            "submitted_at": "2026-05-15T12:00:00+00:00",
            "operator_id": None,
            "deployment_profile": None,
        })