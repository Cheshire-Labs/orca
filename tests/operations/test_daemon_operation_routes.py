"""The daemon serves the operation routes against a real loaded runtime."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import InjectedSystem, create_app


@pytest_asyncio.fixture
async def loaded_client(injected: InjectedSystem) -> AsyncIterator[AsyncClient]:
    """Daemon with the daemon-conftest fixture system pre-loaded."""
    app = create_app(injected=injected)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def empty_client() -> AsyncIterator[AsyncClient]:
    """Daemon with no system loaded; gating contract test target."""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


# -- system-info (regression: the daemon binding must not break it) --------


@pytest.mark.asyncio
async def test_daemon_system_info_still_works(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/api/system")
    assert resp.status_code == 200
    body = resp.json()
    # ``is_simulating`` was retired in canon's sim-hierarchy v3.4.
    assert "name" in body and "description" in body and "version" in body


# -- Thread mutations --------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_pause_execution_not_found(loaded_client: AsyncClient) -> None:
    """Unknown execution -> 404 via the binder's `OperationError.not_found`
    -> `_to_http` mapping. The fixture system has no live executions.
    """
    resp = await loaded_client.post(
        "/api/pause",
        json={"scope": {"kind": "execution", "execution_id": "unknown"}},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_daemon_resume_thread_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.post(
        "/api/resume",
        json={"scope": {"kind": "thread", "execution_id": "e1", "thread_id": "t1"}},
    )
    assert resp.status_code == 404


# -- Execution lifecycle -----------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_list_executions_empty(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/api/executions")
    assert resp.status_code == 200
    assert resp.json() == {"executions": []}


@pytest.mark.asyncio
async def test_daemon_get_execution_detail_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/api/executions/missing")
    assert resp.status_code == 404
    assert resp.json()["detail"]["extras"]["execution_id"] == "missing"


# -- Labware -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_list_labware_empty(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/api/labware-instances")
    assert resp.status_code == 200
    body = resp.json()
    assert "labware" in body and isinstance(body["labware"], list)


@pytest.mark.asyncio
async def test_daemon_get_labware_by_id_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/api/labware-instances/lw_missing")
    assert resp.status_code == 404


# -- Device introspection ----------------------------------------------------


# -- Catalog reads -----------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_list_workflows(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/api/workflows")
    assert resp.status_code == 200
    body = resp.json()
    assert "workflows" in body
    # Fixture system has `simple_workflow` registered (per
    # `tests/test_system_runtime._build_simple_system`).
    assert any(w["name"] == "simple_workflow" for w in body["workflows"])


@pytest.mark.asyncio
async def test_daemon_get_workflow_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/api/workflows/does_not_exist")
    assert resp.status_code == 404


# -- Gating: no system loaded -> 503 ----------------------------------------


@pytest.mark.asyncio
async def test_daemon_pause_gated_when_no_system(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/api/pause",
        json={"scope": {"kind": "execution", "execution_id": "e1"}},
    )
    assert resp.status_code == 503


