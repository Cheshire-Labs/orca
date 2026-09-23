"""Daemon-parity smoke tests.

The orca-core CLI calls the daemon's REST surface, so every Operation has to
be bound there and not only on a hosted deployment. This module verifies the
`/operations/*` daemon routes:

1. Are bound (route registered, not 404).
2. Run the Operation end-to-end against a real loaded runtime.
3. Return the typed Pydantic Response shape.

One representative test per Operation group; the per-operation depth tests
live with the hosted binder. The daemon binder is the same code path as the
hosted REST binder, apart from the error-reshape, so that contract coverage
carries over.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.system_runtime import SystemRuntime


@pytest_asyncio.fixture
async def loaded_client(runtime: SystemRuntime) -> AsyncIterator[AsyncClient]:
    """Daemon with the daemon-conftest fixture system pre-loaded."""
    app = create_app(initial_system_runtime=runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def empty_client() -> AsyncIterator[AsyncClient]:
    """Daemon with no system loaded; gating contract test target."""
    app = create_app(initial_system_runtime=None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


# -- system-info (regression: the daemon binding must not break it) --------


@pytest.mark.asyncio
async def test_daemon_system_info_still_works(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/operations/system-info")
    assert resp.status_code == 200
    body = resp.json()
    # ``is_simulating`` was retired in canon's sim-hierarchy v3.4.
    assert "name" in body and "description" in body and "version" in body


# -- Thread-mutation daemon-parity -------------------------------------------


@pytest.mark.asyncio
async def test_daemon_pause_execution_not_found(loaded_client: AsyncClient) -> None:
    """Unknown execution -> 404 via the binder's `OperationError.not_found`
    -> `_to_http` mapping. The fixture system has no live executions.
    """
    resp = await loaded_client.post(
        "/operations/pause",
        json={"scope": {"kind": "execution", "execution_id": "unknown"}},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_daemon_resume_thread_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.post(
        "/operations/resume",
        json={"scope": {"kind": "thread", "execution_id": "e1", "thread_id": "t1"}},
    )
    assert resp.status_code == 404


# -- Execution-lifecycle daemon-parity ---------------------------------------


@pytest.mark.asyncio
async def test_daemon_list_executions_empty(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/operations/list-executions")
    assert resp.status_code == 200
    assert resp.json() == {"executions": []}


@pytest.mark.asyncio
async def test_daemon_get_execution_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.post(
        "/operations/get-execution",
        json={"execution_id": "missing"},
    )
    assert resp.status_code == 404


# -- Labware daemon-parity ---------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_list_labware_empty(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/operations/list-labware")
    assert resp.status_code == 200
    body = resp.json()
    assert "labware" in body and isinstance(body["labware"], list)


@pytest.mark.asyncio
async def test_daemon_get_labware_by_id_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.post(
        "/operations/get-labware-by-id",
        json={"labware_id": "lw_missing"},
    )
    assert resp.status_code == 404


# -- Device-introspection daemon-parity --------------------------------------


@pytest.mark.asyncio
async def test_daemon_list_devices(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/operations/list-devices")
    assert resp.status_code == 200
    body = resp.json()
    assert "devices" in body and isinstance(body["devices"], list)


# -- Catalog-read daemon-parity ----------------------------------------------


@pytest.mark.asyncio
async def test_daemon_list_workflows(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.get("/operations/list-workflows")
    assert resp.status_code == 200
    body = resp.json()
    assert "workflows" in body
    # Fixture system has `simple_workflow` registered (per
    # `tests/test_system_runtime._build_simple_system`).
    assert any(w["name"] == "simple_workflow" for w in body["workflows"])


@pytest.mark.asyncio
async def test_daemon_get_workflow_not_found(loaded_client: AsyncClient) -> None:
    resp = await loaded_client.post(
        "/operations/get-workflow",
        json={"name": "does_not_exist"},
    )
    assert resp.status_code == 404


# -- Gating: no system loaded -> 503 across surfaces ------------------------


@pytest.mark.asyncio
async def test_daemon_pause_gated_when_no_system(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/operations/pause",
        json={"scope": {"kind": "execution", "execution_id": "e1"}},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_daemon_list_devices_gated_when_no_system(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/operations/list-devices")
    assert resp.status_code == 503
