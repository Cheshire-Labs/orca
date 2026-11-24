"""Wire-shape tests for the v3.4 typed envelope codes on submit routes.

Sim-hierarchy v3.4 introduces three submit-time error codes that escape
through the typed envelope shape (`{detail: {code, message, extras}}`)
rather than `detail: str(e)`:

- RUN_MODE_REQUIRED (422): Pydantic schema rejects before the runtime
  sees the call. Asserted as the Pydantic-422 shape with `loc=(body,
  run_mode)` so a future schema loosening (e.g. `run_mode: str | None
  = None`) is caught before the typed runtime path goes dead.
- RUN_MODE_MISMATCH (409): a JOIN_EXISTING submission whose run_mode
  differs from the live execution's run_mode (it replaced the refuse-all
  `CONCURRENT_SUBMISSION_REFUSED`); envelope carries
  `blocking_execution_id`, `blocking_workflow_name`,
  `existing_run_mode`, `submitted_run_mode`.

A refactor that flattened `detail` to `str(e)` would not fail any
existing test; this file is the regression guard.
"""


import pytest
from httpx import AsyncClient, Response

from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import (
    OtherRunModeActiveError,
    OccupiedSlot,
    RunModeMismatchError,
    StartLocationsOccupiedError,
)
from orca.runtime.system_runtime import SystemRuntime


async def _method_execution_refused_with(
    error: Exception,
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Response:
    async def _stub_submit_method(*args: str, **kwargs: object) -> None:
        del args, kwargs
        raise error

    monkeypatch.setattr(runtime, "submit_method", _stub_submit_method)
    return await client.post(
        "/api/method-executions",
        json={
            "workflow_name": "simple_workflow",
            "method_name": "shake_method",
            "labware_start": {"plate_96": "pad1"},
            "labware_end": {"plate_96": "pad1"},
            "run_mode": "LIVE",
        },
    )


# -- RUN_MODE_REQUIRED -------------------------------------------------------


async def test_run_mode_missing_returns_422(client: AsyncClient) -> None:
    """Missing `run_mode` on the submit body is rejected by the Pydantic
    schema before reaching the typed runtime gate. The 422 is the error
    envelope, with every Pydantic error under `extras.errors` and the
    `run_mode` field named. A schema relaxation that re-routes the
    request through the dead-but-defensive runtime path would change
    this shape and fail the test."""
    resp = await client.post(
        "/api/executions",
        json={"workflow_name": "simple_workflow"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "detail" in body
    error_locs = [tuple(e.get("loc", [])) for e in body["detail"]["extras"]["errors"]]
    assert ("body", "run_mode") in error_locs, (
        f"expected ('body', 'run_mode') in {error_locs!r}"
    )


# -- RUN_MODE_MISMATCH -------------------------------------------------------


async def test_run_mode_mismatch_envelope_wire_shape(
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mismatched run mode answers 409 naming the blocking execution and both modes."""
    exc = RunModeMismatchError(
        blocking_execution_id="exec-abc",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.CHECK_RUN,
        submitted_run_mode=WorkflowRunMode.LIVE,
    )
    resp = await _method_execution_refused_with(exc, runtime, client, monkeypatch)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "run_mode_mismatch"
    assert detail["message"]
    assert detail["extras"] == {
        "blocking_execution_id": "exec-abc",
        "blocking_workflow_name": "smc_assay",
        "existing_run_mode": "CHECK_RUN",
        "submitted_run_mode": "LIVE",
    }


# -- OTHER_RUN_MODE_ACTIVE --------------------------------------------


async def test_other_run_mode_active_envelope_wire_shape(
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system runs in one run mode at a time; the 409 names the running execution and both modes."""
    exc = OtherRunModeActiveError(
        blocking_execution_id="exec-live",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.LIVE,
        submitted_run_mode=WorkflowRunMode.SIM,
    )
    resp = await _method_execution_refused_with(exc, runtime, client, monkeypatch)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "other_run_mode_active"
    assert detail["message"]
    assert detail["extras"] == {
        "blocking_execution_id": "exec-live",
        "blocking_workflow_name": "smc_assay",
        "existing_run_mode": "LIVE",
        "submitted_run_mode": "SIM",
    }


# -- START_LOCATION_OCCUPIED -------------------------------------------------


async def test_post_method_executions_occupied_start_returns_typed_409(
    runtime: SystemRuntime,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /method-executions catches StartLocationsOccupiedError and answers
    the typed 409.

    A standalone method brings its own labware, so it meets the same
    start-location check a workflow submission does. The route caught only
    KeyError / TypeError / ValueError, and the error is a RuntimeError, so it
    escaped as a bare 500 with no slot in it for the operator to clear. The
    engine-side half of this is pinned by
    tests/test_standalone_method_submission_e2e.py.
    """
    async def _stub_submit_method(*args: str, **kwargs: object) -> None:
        del args, kwargs
        raise StartLocationsOccupiedError([
            OccupiedSlot(
                position_id="pad1",
                existing_labware_id="8d727600",
                existing_labware_name="plate_96-8d727600",
                existing_template_name="plate_96",
            ),
        ])

    monkeypatch.setattr(runtime, "submit_method", _stub_submit_method)
    resp = await client.post(
        "/api/method-executions",
        json={
            "workflow_name": "simple_workflow",
            "method_name": "shake_method",
            "labware_start": {"plate_96": "pad1"},
            "labware_end": {"plate_96": "pad1"},
            "run_mode": "CHECK_RUN",
        },
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    # The value a hosted deployment's ErrorCode carries and this repo's submit-execution
    # binder emits. The route used to send the enum NAME instead.
    assert detail["code"] == "start_location_occupied"
    assert "pad1" in detail["message"]
    assert detail["extras"]["occupied"] == [
        {
            "position_id": "pad1",
            "existing_labware_id": "8d727600",
            "existing_labware_name": "plate_96-8d727600",
            "existing_template_name": "plate_96",
            "source": "unknown",
        },
    ]
