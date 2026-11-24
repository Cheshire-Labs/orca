"""Tests for SubmitExecutionOperation.

Covers:
- Unit tests with mocked runtime: every shape goes through submit_group,
  variable coercion, error mapping (KeyError → not_found,
  ValueError → invalid_input, RuntimeError → conflict).
- Integration through the daemon's POST /executions, which runs the Operation.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import InjectedSystem, create_app
from orca.operations.protocol import OperationError
from orca.operations.error_codes import ErrorCode
from orca.operations.submission import SubmitExecutionOperation, SubmitMethodExecutionOperation
from orca.operations.submission_models import MethodExecutionRequest, SubmitExecutionRequest
from orca.operations.wire_models import LabwareGroupDTO
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.status_models import SubmissionSnapshot, SubmissionStatus
from orca.runtime.system_runtime import SystemRuntime
from orca.variables.errors import SubmissionInputsRefusedError, VariableValidationError
from orca.workflow_models.labware_group import AcquisitionValidationError
from orca.workflow_models.submission import BatchMode


# -- Fixtures + helpers -------------------------------------------------------


def _make_snapshot(
    *,
    submission_id: str = "sub-1",
    execution_id: str = "exec-1",
    workflow_name: str = "hello",
    group_count: int = 1,
    batch_mode: BatchMode = BatchMode.STANDALONE,
    run_mode: WorkflowRunMode = WorkflowRunMode.CHECK_RUN,
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
    runtime.submissions.submit_group = AsyncMock(return_value=snapshot)
    runtime.get_execution = MagicMock(return_value=ExecutionRecord(
        id=snapshot.execution_id,
        workflow_name=snapshot.workflow_name,
        status=ExecutionState.RUNNING,
    ))
    runtime.unsettled_state = AsyncMock(return_value=[])
    return runtime


@pytest_asyncio.fixture
async def loaded_client(injected: InjectedSystem) -> AsyncIterator[AsyncClient]:
    app = create_app(injected=injected)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


# -- Unit tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_profile_alone_goes_through_submit_group_with_no_groups() -> None:
    runtime = _make_mock_runtime(_make_snapshot())
    op = SubmitExecutionOperation(runtime=runtime)

    await op.run(SubmitExecutionRequest(
        workflow_name="wf", deployment_profile="qc", run_mode="CHECK_RUN",
    ))

    call = runtime.submissions.submit_group.await_args
    assert call.kwargs["deployment_profile"] == "qc"
    assert call.kwargs["groups"] == ()


@pytest.mark.asyncio
async def test_no_groups_routes_through_submit_group_with_no_groups() -> None:
    """No groups + STANDALONE + no operator metadata → submit_group with groups=()."""
    snap = _make_snapshot()
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    resp = await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        variables={"plate_count": 1},
        run_mode="CHECK_RUN",
    ))
    assert resp.id == "sub-1"
    assert resp.execution_id == "exec-1"
    runtime.submissions.submit_group.assert_awaited_once()
    assert runtime.submissions.submit_group.await_args.kwargs["groups"] == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [
    SubmissionInputsRefusedError(["speed has no value and no default."]),
    AcquisitionValidationError("Nothing says which tips rack_1 holds."),
])
async def test_a_refused_submit_on_the_default_path_answers_validation_error(refusal: ValueError) -> None:
    runtime = _make_mock_runtime(_make_snapshot())
    runtime.submissions.submit_group = AsyncMock(side_effect=refusal)
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as raised:
        await op.run(SubmitExecutionRequest(workflow_name="hello", run_mode="CHECK_RUN"))

    assert raised.value.code is ErrorCode.VALIDATION_ERROR


_INPUT_REFUSALS = [
    SubmissionInputsRefusedError(["speed has no value and no default."]),
    AcquisitionValidationError("Nothing says which tips rack_1 holds."),
    VariableValidationError("speed", 5000, "above maximum 2000.0"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", _INPUT_REFUSALS)
async def test_a_refused_submit_on_the_group_path_answers_validation_error(refusal: ValueError) -> None:
    runtime = _make_mock_runtime(_make_snapshot())
    runtime.submissions.submit_group = AsyncMock(side_effect=refusal)
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as raised:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello", batch_mode=BatchMode.JOIN_EXISTING, run_mode="CHECK_RUN",
        ))

    assert raised.value.code is ErrorCode.VALIDATION_ERROR
    assert raised.value.surface_code() == "validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", _INPUT_REFUSALS)
async def test_a_refused_standalone_method_run_answers_validation_error(refusal: ValueError) -> None:
    runtime = MagicMock()
    runtime.submissions.submit_method = AsyncMock(side_effect=refusal)
    op = SubmitMethodExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as raised:
        await op.run(MethodExecutionRequest(
            workflow_name="hello", method_name="add", labware_start={}, labware_end={}, run_mode="CHECK_RUN",
        ))

    assert raised.value.code is ErrorCode.VALIDATION_ERROR
    assert raised.value.surface_code() == "validation_error"


@pytest.mark.asyncio
async def test_multi_group_shape_routes_through_submissions_submit_group() -> None:
    """groups != None → submissions.submit_group path."""
    snap = _make_snapshot(group_count=2)
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    resp = await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        groups=[
            LabwareGroupDTO.model_validate({"id": "g1", "members": [
                {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
            ]}),
            LabwareGroupDTO.model_validate({"id": "g2", "members": [
                {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
            ]}),
        ],
        run_mode="CHECK_RUN",
    ))
    assert resp.group_count == 2
    runtime.submissions.submit_group.assert_awaited_once()
    assert len(runtime.submissions.submit_group.await_args.kwargs["groups"]) == 2


@pytest.mark.asyncio
async def test_operator_id_forces_multi_group_path() -> None:
    """operator_id != None → multi-group path even with no groups."""
    snap = _make_snapshot(operator_id="alice")
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        operator_id="alice",
        run_mode="CHECK_RUN",
    ))
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
        run_mode="CHECK_RUN",
    ))
    runtime.submissions.submit_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_mode_string_parsed_to_enum() -> None:
    snap = _make_snapshot(run_mode=WorkflowRunMode.SIM)
    runtime = _make_mock_runtime(snap)
    op = SubmitExecutionOperation(runtime=runtime)

    await op.run(SubmitExecutionRequest(
        workflow_name="hello",
        groups=[LabwareGroupDTO.model_validate({"id": "g1", "members": [
            {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
        ]})],
        run_mode="SIM",
    ))
    call = runtime.submissions.submit_group.await_args
    assert call.kwargs["mode"] == WorkflowRunMode.SIM


@pytest.mark.asyncio
async def test_no_groups_keyerror_maps_to_not_found() -> None:
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock(side_effect=KeyError("unknown_workflow"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="unknown_workflow", run_mode="CHECK_RUN",
        ))
    assert excinfo.value.http_status == 404


@pytest.mark.asyncio
async def test_no_groups_runtime_error_maps_to_conflict() -> None:
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock(side_effect=RuntimeError("invalid state"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello", run_mode="CHECK_RUN",
        ))
    assert excinfo.value.http_status == 409


@pytest.mark.asyncio
async def test_multi_group_keyerror_maps_to_not_found() -> None:
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock(side_effect=KeyError("missing_template"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello",
            groups=[LabwareGroupDTO.model_validate({"id": "g1", "members": [
                {"thread_template_name": "missing_template", "acquisition": {"kind": "pool"}}
            ]})],
            run_mode="CHECK_RUN",
        ))
    assert excinfo.value.http_status == 404


@pytest.mark.asyncio
async def test_multi_group_valueerror_maps_to_invalid_input() -> None:
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock(side_effect=ValueError("bad input"))
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello",
            groups=[LabwareGroupDTO.model_validate({"id": "g1", "members": [
                {"thread_template_name": "t1", "acquisition": {"kind": "pool"}}
            ]})],
            run_mode="CHECK_RUN",
        ))
    assert excinfo.value.http_status == 422


@pytest.mark.asyncio
async def test_barcode_acquisition_without_barcode_raises_invalid_input() -> None:
    """Acquisition shape validation happens inside run() during group build."""
    runtime = MagicMock()
    runtime.submissions.submit_group = AsyncMock()
    op = SubmitExecutionOperation(runtime=runtime)

    with pytest.raises(OperationError) as excinfo:
        await op.run(SubmitExecutionRequest(
            workflow_name="hello",
            groups=[LabwareGroupDTO.model_validate({"id": "g1", "members": [
                {"thread_template_name": "t1",
                 "acquisition": {"kind": "barcode"}}  # missing barcode
            ]})],
            run_mode="CHECK_RUN",
        ))
    assert excinfo.value.http_status == 422
    runtime.submissions.submit_group.assert_not_awaited()


# -- Pydantic validation tests ------------------------------------------------


def test_request_rejects_non_primitive_variable_values() -> None:
    """Variables must be `OptionValue` (str|int|float|bool)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitExecutionRequest(
            workflow_name="hello",
            variables={"bad": [1, 2, 3]},  # type: ignore[dict-item]
            run_mode="CHECK_RUN",
        )


def test_request_rejects_extras() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitExecutionRequest(
            workflow_name="hello",
            unknown_field="boom",  # type: ignore[call-arg]
            run_mode="CHECK_RUN",
        )


def test_request_requires_run_mode() -> None:
    """Sim-hierarchy v3.4: ``run_mode`` has no default; Pydantic rejects
    the request at body-parse time when omitted."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubmitExecutionRequest(workflow_name="hello")  # type: ignore[call-arg]


# -- Integration through the daemon's POST /executions ----------------------


@pytest.mark.asyncio
async def test_daemon_submit_execution_legacy_route(loaded_client: AsyncClient) -> None:
    # sim-hierarchy v3.4: ``run_mode`` is required on every submission;
    # the runtime raises ``RunModeRequiredError`` without it.
    payload = {"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"}
    resp = await loaded_client.post("/api/executions", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workflow_name"] == "simple_workflow"
    # A submission with no groups stamps group_count=0; one with groups
    # produces group_count >= 1.
    assert body["group_count"] == 0
    assert body["batch_mode"] == "STANDALONE"
    assert "id" in body and "execution_id" in body


@pytest.mark.asyncio
async def test_daemon_submit_execution_missing_workflow_returns_404(
    loaded_client: AsyncClient,
) -> None:
    resp = await loaded_client.post(
        "/api/executions",
        json={"workflow_name": "does_not_exist", "run_mode": "CHECK_RUN"},
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
        "/api/executions",
        json={"workflow_name": "simple_workflow", "variables": {"bad": [1, 2]}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_daemon_submit_execution_503_when_no_system() -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        resp = await c.post(
            "/api/executions",
            json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_a_submission_that_asks_for_a_barcode_check_nothing_does_is_refused(
    loaded_client: AsyncClient,
) -> None:
    resp = await loaded_client.post("/api/executions", json={
        "workflow_name": "hello",
        "run_mode": "CHECK_RUN",
        "groups": [{"id": "g1", "members": [{
            "thread_template_name": "t1",
            "acquisition": {"kind": "location", "source_location": "stack_1", "verify_barcode": "ABC123"},
        }]}],
    })

    assert resp.status_code == 422, resp.text
    assert "verify_barcode is not supported" in resp.json()["detail"]["message"]
