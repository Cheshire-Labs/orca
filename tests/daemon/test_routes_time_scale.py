"""The daemon exposes a simulated run's time scale: on submit, on the record, and
as an operation that changes it while the run is going."""

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import ValidationError

from orca.daemon.routes import _default_sim_time_scale_from_env
from orca.daemon.schemas import SubmitWorkflowRequest
from orca.operations.execution_models import SetExecutionTimeScaleRequest


async def test_a_submission_names_its_scale_and_the_record_shows_it(client: AsyncClient) -> None:
    resp = await client.post(
        "/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM", "sim_time_scale": 0.5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sim_time_scale"] == 0.5
    execution_id = resp.json()["id"]

    changed = await client.post(
        "/operations/set-execution-time-scale",
        json={"execution_id": execution_id, "sim_time_scale": 0.0},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["sim_time_scale"] == 0.0

    detail = await client.post(
        "/operations/get-execution-detail", json={"execution_id": execution_id},
    )
    assert detail.status_code == 200
    assert detail.json()["sim_time_scale"] == 0.0


async def test_a_scale_on_a_live_submission_is_refused_with_its_own_code(client: AsyncClient) -> None:
    resp = await client.post(
        "/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "LIVE", "sim_time_scale": 1.0},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "SIM_TIME_SCALE_NOT_APPLICABLE"


@pytest.mark.parametrize("bad", [-1, True])
async def test_a_scale_that_is_not_a_finite_number_at_least_zero_is_422(
    client: AsyncClient, bad: float,
) -> None:
    resp = await client.post(
        "/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM", "sim_time_scale": bad},
    )
    assert resp.status_code == 422, resp.text
    changed = await client.post(
        "/operations/set-execution-time-scale",
        json={"execution_id": "any", "sim_time_scale": bad},
    )
    assert changed.status_code == 422, changed.text


def test_an_infinite_scale_is_refused_by_the_request_models() -> None:
    for model, body in (
        (SubmitWorkflowRequest, {"workflow_name": "w", "run_mode": "PURE_SIM", "sim_time_scale": float("inf")}),
        (SetExecutionTimeScaleRequest, {"execution_id": "e", "sim_time_scale": float("inf")}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(body)


def test_the_deployment_default_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCA_SIM_TIME_SCALE", raising=False)
    assert _default_sim_time_scale_from_env() == 0.0
    monkeypatch.setenv("ORCA_SIM_TIME_SCALE", " 0.5 ")
    assert _default_sim_time_scale_from_env() == 0.5
    for bad in ("abc", "-1", "inf"):
        monkeypatch.setenv("ORCA_SIM_TIME_SCALE", bad)
        with pytest.raises(HTTPException) as refused:
            _default_sim_time_scale_from_env()
        assert refused.value.status_code == 400


async def test_setting_the_scale_of_an_unknown_execution_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/operations/set-execution-time-scale",
        json={"execution_id": "does-not-exist", "sim_time_scale": 1.0},
    )
    assert resp.status_code == 404
