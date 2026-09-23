"""Tests for the pre-load state: daemon is up but no system is loaded.

`_require_system_runtime` is the shared helper on every execution/thread/
reservation route. If it's missing from a route, that route would return 500
or silently accept the no-system-loaded state. Each parametrized case
exercises one route path + method so a regression on any one of them is
caught.

/health, /shutdown, /mount-topology are NOT gated (that's their whole point
pre-load) and /unload is the complementary case: must 409 because there's
nothing to unload. Those are separately tested here.

Tests use the `empty_client` fixture from conftest -- daemon constructed
with `initial_system_runtime=None`.
"""

import pytest
from httpx import AsyncClient

from orca.daemon.schemas import ErrorResponse


# (http_method, url, body) for every route gated by _require_system_runtime.
# A regression that removed the gate from any one of these would flip the
# expected 409 into something else, and the parametrized assertion catches it.
# The legacy thread-mutation and execution-read URLs are gone from the
# daemon. Their replacements live on the unified
# Operations surface (POST /operations/<op>) and surface "no system
# loaded" as ``service_unavailable`` (503) via the OperationError
# binder, not 409 via ``_require_system_runtime``. Pre-load coverage
# for the operations routes lives in
# ``tests/operations/test_daemon_operation_parity.py``.
_GATED_ROUTES = [
    # POST /executions kept (legacy submit_workflow handler); canon added
    # the now-required ``run_mode`` field to the body (sim-hierarchy v3.4).
    # GET /executions, GET /executions/{id}, DELETE /executions/{id} were
    # retired -- their pre-load coverage lives on the
    # Operations surface in tests/operations/test_daemon_operation_parity.py.
    ("POST", "/executions", {"workflow_name": "whatever", "run_mode": "PURE_SIM"}),
    ("GET", "/executions/some-id/threads", None),
    ("GET", "/executions/some-id/reservations", None),
]


@pytest.mark.parametrize(("method", "url", "body"), _GATED_ROUTES)
async def test_gated_routes_return_409_when_no_system_loaded(
    empty_client: AsyncClient, method: str, url: str, body: dict | None,
) -> None:
    """Every execution/thread route must return 409 before any load.

    If someone adds a new route that operates on a loaded system but forgets
    to call `_require_system_runtime`, add it to `_GATED_ROUTES` and this
    parametrized test will catch the regression.
    """
    if body is None:
        resp = await empty_client.request(method, url)
    else:
        resp = await empty_client.request(method, url, json=body)
    assert resp.status_code == 409, (
        f"{method} {url} should return 409 when no system is loaded, got {resp.status_code}"
    )
    err = ErrorResponse.model_validate(resp.json())
    assert "no system loaded" in err.detail.lower()


async def test_unload_with_nothing_loaded_returns_409(
    empty_client: AsyncClient,
) -> None:
    """/unload has the opposite gate: it fails when there's NOTHING to unload.

    The message should distinguish 'nothing to unload' from 'no system loaded'
    so operators can tell these two pre-load-like states apart."""
    resp = await empty_client.post("/unload")
    assert resp.status_code == 409
    err = ErrorResponse.model_validate(resp.json())
    assert "nothing to unload" in err.detail.lower()


async def test_health_works_with_no_system_loaded(
    empty_client: AsyncClient,
) -> None:
    """/health must NOT be gated. Pre-load health is the primary daemon
    liveness check; if it 409's, `orca start` polling would fail."""
    resp = await empty_client.get("/health")
    assert resp.status_code == 200
