"""The daemon before anything is mounted: run reads answer 503 `runtime_not_ready`, and unload has nothing to do."""

import pytest
from httpx import AsyncClient



@pytest.mark.parametrize("url", ["/api/executions/some-id/threads/some-thread", "/api/reservations?execution_id=some-id"])
async def test_a_run_read_with_no_system_loaded_answers_runtime_not_ready(
    empty_client: AsyncClient, url: str,
) -> None:
    resp = await empty_client.get(url)

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"


async def test_unload_with_nothing_loaded_returns_409(
    empty_client: AsyncClient,
) -> None:
    """/unload has the opposite gate: it fails when there's NOTHING to unload.

    The message should distinguish 'nothing to unload' from 'no system loaded'
    so operators can tell these two pre-load-like states apart."""
    resp = await empty_client.post("/api/runtime/unload")
    assert resp.status_code == 409
    err = resp.json()["detail"]
    assert "nothing to unload" in err["message"].lower()


async def test_health_works_with_no_system_loaded(
    empty_client: AsyncClient,
) -> None:
    """/health must NOT be gated. Pre-load health is the primary daemon
    liveness check; if it 409's, `orca start` polling would fail."""
    resp = await empty_client.get("/health")
    assert resp.status_code == 200
