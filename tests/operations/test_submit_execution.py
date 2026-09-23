"""Tests for SubmitExecutionOperation.

Covers:
- Unit tests with mocked runtime: legacy dispatch, multi-group dispatch,
  variable coercion, error mapping (KeyError → not_found,
  ValueError → invalid_input, RuntimeError → conflict).
- Integration through the orca-core daemon binder at
  POST /operations/submit-execution.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.submission import SubmitExecutionOperation
from orca.operations.submission_models import SubmitExecutionRequest
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import SubmissionSnapshot, SubmissionStatus
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime


# -- Fixtures + helpers -------------------------------------------------------


def _make_snapshot(
    *,
    submission_id: str = "sub-1",
    execution_id: str = "exec-1",
    workflow_name: str = "hello",
    group_count: int = 1,
    batch_mode: BatchMode = BatchMode.STANDALONE,
    run_mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM,
    operator_id: str | None = None,
    deployment_profile: str | None = None,
) -> SubmissionSnapshot:
    return SubmissionSnapshot(
        id=submission_id,
        execution_id=execution_id,
        workflow_name=workflow_name,
        group_count=group_count,
        status=SubmissionStatus.ACCEPTED,
        batch_mode=batch_mode,
        submitted_at=datetime.now(timezone.utc).isoformat(),
        run_mode=run_mode,
        operator_id=operator_id,
        deployment_profile=deployment_profile,
    )


def _make_mock_runtime(snapshot: SubmissionSnapshot) -> MagicMock:
    runtime = MagicMock()
    runtime.submit_workflow = AsyncMock(
        return_value=ExecutionRecord(
            id=snapshot.execution_id,
            workflow_name=snapshot.workflow_name,
            status=ExecutionState.RUNNING,
        ),
    )
    runtime.submissions.submit_group = AsyncMock(return_value=snapshot)
    runtime.submissions.list_submissions = MagicMock(return_value=[snapshot])
    return runtime


@pytest_asyncio.fixture
async def loaded_client(runtime: SystemRuntime) -> AsyncIterator[AsyncClient]:
    app = create_app(initial_system_runtime=runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


# -- Unit tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_shape_routes_through_submit_workflow() -> None:
    """No groups + STANDALONE + no operator metadata → submit_workflow path."""
    snap = _make_snapshot()
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    resp = await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        variables={"plate_count": 1},
        run_mode="PURE_SIM",
    ))
    assert resp.id == "sub-1"
    assert resp.execution_id == "exec-1"
    runtime.submit_workflow.assert_awaited_once()
    runtime.submissions.submit_group.assert_not_awaited()
    runtime.submissions.list_submissions.assert_called_once_with(execution_id="exec-1")


@pytest.mark.asyncio
async def test_multi_group_shape_routes_through_submissions_submit_group() -> None:
    """groups != None → submissions.submit_group path."""
    snap = _make_snapshot(group_count=2)
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    resp = await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        groups=[
            {"id": "g1", "members": [
                {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
            ]},
            {"id": "g2", "members": [
                {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
            ]},
        ],
        run_mode="PURE_SIM",
    ))
    assert resp.group_count == 2
    runtime.submit_workflow.assert_not_awaited()
    runtime.submissions.submit_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_operator_id_forces_multi_group_path() -> None:
    """operator_id != None → multi-group path even with no groups."""
    snap = _make_snapshot(operator_id="alice")
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        operator_id="alice",
        run_mode="PURE_SIM",
    ))
    runtime.submit_workflow.assert_not_awaited()
    runtime.submissions.submit_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_existing_forces_multi_group_path() -> None:
    """batch_mode=JOIN_EXISTING → multi-group path."""
    snap = _make_snapshot(batch_mode=BatchMode.JOIN_EXISTING)
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        batch_mode="JOIN_EXISTING",
        run_mode="PURE_SIM",
    ))
    runtime.submit_workflow.assert_not_awaited()
    runtime.submissions.submit_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_mode_string_parsed_to_enum() -> None:
    snap = _make_snapshot(run_mode=WorkflowRunMode.DEVICE_SIM)
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        groups=[{"id": "g1", "members": [
            {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
        ]}],
        run_mode="DEVICE_SIM",
    ))
    call = runtime.submissions.submit_group.await_args
    assert call.kwargs["mode"] == WorkflowRunMode.DEVICE_SIM


@pytest.mark.asyncio
async def test_legacy_keyerror_maps_to_not_found() -> None:
    runtime = MagicMock()
    runtime.submit_workflow = AsyncMock(side_effect=KeyError("unknown_workflow"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="unknown_workflow", run_mode="PURE_SIM",
        ))
    assert excinfo.value.code == OperationErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_legacy_runtime_error_maps_to_conflict() -> None:
    runtime = MagicMock()
    runtime.submit_workflow = AsyncMock(side_effect=RuntimeError("invalid state"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello", run_mode="PURE_SIM",
        ))
    assert excinfo.value.code == OperationErrorCode.CONFLICT


@pytest.mark.asyncio
async def test_multi_group_keyerror_maps_to_not_found() -> None:
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock(side_effect=KeyError("missing_template"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello",
            groups=[{"id": "g1", "members": [
                {"thread_template_name": "missing_template", "acquisition": {"kind": "pool"}}
            ]}],
            run_mode="PURE_SIM",
        ))
    assert excinfo.value.code == OperationErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_multi_group_valueerror_maps_to_invalid_input() -> None:
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock(side_effect=ValueError("bad input"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello",
            groups=[{"id": "g1", "members": [
                {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
            ]}],
            run_mode="PURE_SIM",
        ))
    assert excinfo.value.code == OperationErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_barcode_acquisition_without_barcode_raises_invalid_input() -> None:
    """Acquisition shape validation happens inside run() during group build."""
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock()
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello",
            groups=[{"id": "g1", "members": [
                {"thread_template_name": "t1",
                 "acquisition": {"kind": "barcode"}}  # missing barcode
            ]}],
            run_mode="PURE_SIM",
        ))
    assert excinfo.value.code == OperationErrorCode.INVALID_INPUT
    runtime.submissions.submit_group.assert_not_awaited()


# -- Pydantic validation tests ------------------------------------------------


def test_request_rejects_non_primitive_variable_values() -> None:
    """Variables must be `OptionValue` (str|int|float|bool)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitExecutionRequest(
            workflow_name="hello",
            variables={"bad": [1, 2, 3]},  # type: ignore[dict-item]
            run_mode="PURE_SIM",
        )


def test_request_rejects_extras() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitExecutionRequest(
            workflow_name="hello",
            unknown_field="boom",  # type: ignore[call-arg]
            run_mode="PURE_SIM",
        )


def test_request_requires_run_mode() -> None:
    """Sim-hierarchy v3.4: ``run_mode`` has no default; Pydantic rejects
    the request at body-parse time when omitted."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitExecutionRequest(workflow_name="hello")  # type: ignore[call-arg]


# -- Integration through orca-core daemon binder -----------------------------


@pytest.mark.asyncio
async def test_daemon_submit_execution_legacy_route(loaded_client: AsyncClient) -> None:
    # sim-hierarchy v3.4: ``run_mode`` is required on every submission;
    # the runtime raises ``RunModeRequiredError`` without it.
    payload = {"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"}
    resp = await loaded_client.post("/operations/submit-execution", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workflow_name"] == "simple_workflow"
    # Legacy single-workflow path stamps group_count=0 -- there are no
    # LabwareGroups in this submission shape; the multi-group path is
    # the one that produces group_count >= 1.
    assert body["group_count"] == 0
    assert body["batch_mode"] == "STANDALONE"
    assert "id" in body and "execution_id" in body


@pytest.mark.asyncio
async def test_daemon_submit_execution_missing_workflow_returns_404(
    loaded_client: AsyncClient,
) -> None:
    resp = await loaded_client.post(
        "/operations/submit-execution",
        json={"workflow_name": "does_not_exist", "run_mode": "PURE_SIM"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_daemon_submit_execution_invalid_body_returns_422(
    loaded_client: AsyncClient,
) -> None:
    """Pydantic body validation maps to FastAPI's standard 422.

    With `request_model=SubmitExecutionRequest`, FastAPI parses + validates
    the body before the Operation runs; the standard 422 envelope surfaces
    on validation failures (instead of the legacy hand-coded 400).
    """
    resp = await loaded_client.post(
        "/operations/submit-execution",
        json={"workflow_name": "simple_workflow", "variables": {"bad": [1, 2]}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_daemon_submit_execution_503_when_no_system() -> None:
    app = create_app(initial_system_runtime=None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        resp = await c.post(
            "/operations/submit-execution",
            json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
        )
    assert resp.status_code == 503
