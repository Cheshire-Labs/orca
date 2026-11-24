"""The daemon exposes a sim run's speed-up: on submit, on the record, and
as an operation that changes it while the run is going."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from orca.daemon.app import create_app, default_sim_speedup_from_env
from orca.operations.execution_models import SetExecutionSimSpeedupRequest
from orca.operations.protocol import OperationError
from orca.operations.submission_models import SubmitExecutionRequest
from tests.memory_activity import memory_daemon_system
from tests.runtime.manual_place_fixtures import _live_connection_source
from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def sim_client() -> AsyncIterator[AsyncClient]:
    """A daemon whose devices are connected, so it admits a sim run."""
    system, _ = await _build_simple_system()
    injected = await memory_daemon_system(system, connection_source=_live_connection_source())
    await injected.runtime.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(injected=injected)), base_url="http://daemon.test") as c:
            yield c
    finally:
        await injected.runtime.shutdown(confirm=True)


async def test_a_submission_names_its_speedup_and_the_record_shows_it(sim_client: AsyncClient) -> None:
    client = sim_client
    resp = await client.post(
        "/api/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "SIM", "sim_speedup": 2.0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sim_speedup"] == 2.0
    execution_id = resp.json()["execution_id"]

    changed = await client.put(f"/api/executions/{execution_id}/sim-speedup", json={"sim_speedup": 1000.0})
    assert changed.status_code == 200, changed.text
    assert changed.json()["sim_speedup"] == 1000.0

    detail = await client.get(f"/api/executions/{execution_id}")
    assert detail.status_code == 200
    assert detail.json()["sim_speedup"] == 1000.0


async def test_the_execution_list_says_a_running_sim_run_is_simulated_and_shows_its_speedup(sim_client: AsyncClient) -> None:
    client = sim_client
    resp = await client.post(
        "/api/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "SIM", "sim_speedup": 2.0},
    )
    assert resp.status_code == 200, resp.text
    execution_id = resp.json()["execution_id"]

    listed = await client.get("/api/executions")

    assert listed.status_code == 200, listed.text
    row = next(e for e in listed.json()["executions"] if e["id"] == execution_id)
    assert (row["simulated"], row["sim_speedup"]) == (True, 2.0)


@pytest.mark.parametrize("run_mode", ["LIVE", "CHECK_RUN"])
async def test_a_speedup_on_a_live_or_check_run_submission_is_refused_with_its_own_code(
    client: AsyncClient, run_mode: str,
) -> None:
    resp = await client.post(
        "/api/executions",
        json={"workflow_name": "simple_workflow", "run_mode": run_mode, "sim_speedup": 1.0},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "sim_speedup_not_applicable"


@pytest.mark.parametrize("bad", [0, 0.05, 1001, -1, True])
async def test_a_speedup_outside_its_range_or_not_a_number_is_422(
    client: AsyncClient, bad: float,
) -> None:
    resp = await client.post(
        "/api/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN", "sim_speedup": bad},
    )
    assert resp.status_code == 422, resp.text
    changed = await client.put("/api/executions/any/sim-speedup", json={"sim_speedup": bad})
    assert changed.status_code == 422, changed.text


def test_an_infinite_speedup_is_refused_by_the_request_models() -> None:
    for model, body in (
        (SubmitExecutionRequest, {"workflow_name": "w", "run_mode": "CHECK_RUN", "sim_speedup": float("inf")}),
        (SetExecutionSimSpeedupRequest, {"execution_id": "e", "sim_speedup": float("inf")}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(body)


def test_a_framework_sim_runs_a_hundred_times_faster_when_nothing_names_a_speedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCA_SIM_SPEEDUP", raising=False)
    assert default_sim_speedup_from_env() == 100.0


def test_off_means_a_simulated_run_does_not_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_SIM_SPEEDUP", " off ")
    assert default_sim_speedup_from_env() is None
    monkeypatch.setenv("ORCA_SIM_SPEEDUP", "OFF")
    assert default_sim_speedup_from_env() is None
    monkeypatch.setenv("ORCA_SIM_SPEEDUP", "  ")
    assert default_sim_speedup_from_env() == 100.0


def test_the_deployment_default_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_SIM_SPEEDUP", " 50 ")
    assert default_sim_speedup_from_env() == 50.0
    for bad in ("abc", "-1", "0", "inf"):
        monkeypatch.setenv("ORCA_SIM_SPEEDUP", bad)
        with pytest.raises(OperationError) as refused:
            default_sim_speedup_from_env()
        assert refused.value.http_status == 422
        assert refused.value.surface_code() == "validation_error"


async def test_setting_the_speedup_of_an_unknown_execution_is_404(client: AsyncClient) -> None:
    resp = await client.put("/api/executions/does-not-exist/sim-speedup", json={"sim_speedup": 1.0})
    assert resp.status_code == 404
