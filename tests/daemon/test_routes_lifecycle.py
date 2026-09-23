"""Route tests for /health, /shutdown, /unload. Each test targets a
specific bug-shape.

All response bodies are parsed through their Pydantic DTOs -- no
`resp.json()["magic_key"]` lookups. Enum assertions use the real enum
members instead of magic strings, so typos fail at import time.

- Observable state change: POST /shutdown must actually mutate state
  (SystemRuntime shut down AND cleared from app.state), and GET /health
  must actually read app.state (not hard-code values). Probing health
  before AND after shutdown proves both, since a hard-coded value can only
  be one.
- Teardown correctness: after /shutdown unloads the system, a subsequent
  submit must return 409 (no system loaded). Catches regressions where
  the shutdown path forgets to clear app.state.system_runtime.
"""

from httpx import AsyncClient

from orca.daemon.schemas import (
    ErrorResponse,
    HealthResponse,
)
from orca.runtime.system_runtime import RuntimeState, SystemRuntime


async def test_health_tracks_shutdown_transition(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    """/health must reflect live app.state, not cached/hard-coded values.

    Before /shutdown: system_loaded=True, runtime_state=RuntimeState.RUNNING.
    After /shutdown:  system_loaded=False, runtime_state=None (runtime was
    cleared). A hard-coded health response could only satisfy one of these
    two snapshots, so asserting both rules that bug out.
    """
    r_running = await client.get("/health")
    assert r_running.status_code == 200
    body_running = HealthResponse.model_validate(r_running.json())
    assert body_running.system_loaded is True
    assert body_running.runtime_state == RuntimeState.RUNNING

    await client.post("/shutdown")
    # The runtime instance the test holds is now stopped ...
    assert runtime.state == RuntimeState.STOPPED
    # ... AND it's been cleared from the app so future requests see "not loaded".

    r_stopped = await client.get("/health")
    assert r_stopped.status_code == 200
    body_stopped = HealthResponse.model_validate(r_stopped.json())
    assert body_stopped.system_loaded is False
    assert body_stopped.runtime_state is None


async def test_submit_after_shutdown_unloads_returns_409(
    client: AsyncClient,
) -> None:
    """After /shutdown unloads the system, submit must return a 409 carrying
    the 'no system loaded' detail -- not 500, and not a different 409 reason.

    Distinguishing the specific detail matters: this catches a regression
    where /shutdown stops the SystemRuntime but forgets to clear
    app.state.system_runtime. In that case submit would still 409, but
    with a 'Runtime is not running' detail from the RuntimeError branch.
    """
    await client.post("/shutdown")
    resp = await client.post(
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert resp.status_code == 409
    body = ErrorResponse.model_validate(resp.json())
    assert "no system loaded" in body.detail.lower()
