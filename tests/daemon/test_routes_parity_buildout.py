"""Daemon route tests for the backend-parity build-out.

Each route here serves the same DTO shape its cloud-backend counterpart
serves, so the in-tree CLI reaches the unified surface from a single
deployment, with no hosted service in the path. Routes covered:

- GET  /operations/list-methods  (already bound; asserted for completeness)
- POST /operations/get-method
- POST /operations/get-workflow
- GET  /topology  (+ ?source=true)
- GET  /runtime/status
- POST /operations/list-ops-history
- POST /operations/search-ops-history
- POST /operations/get-labware-journey
- POST /labware/runtime/{clear-submission,discharge,clear-all}
"""

from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_get_method_returns_summary(client: AsyncClient) -> None:
    # shake_method exists in both test workflows; disambiguate by workflow.
    resp = await client.post(
        "/operations/get-method",
        json={"name": "shake_method", "workflow_name": "simple_workflow"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["method"]
    assert body["name"] == "shake_method"
    assert body["workflow_name"] == "simple_workflow"
    assert "failure_policy" in body


@pytest.mark.asyncio
async def test_get_method_unknown_is_404(client: AsyncClient) -> None:
    resp = await client.post("/operations/get-method", json={"name": "nope"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_get_workflow_returns_summary(client: AsyncClient) -> None:
    resp = await client.post("/operations/get-workflow", json={"name": "simple_workflow"})
    assert resp.status_code == 200, resp.text
    body = resp.json()["workflow"]
    assert body["name"] == "simple_workflow"
    assert body["entry_thread_template_names"]  # non-empty entry-thread list


@pytest.mark.asyncio
async def test_topology_view_lists_topology_entries(client: AsyncClient) -> None:
    resp = await client.get("/topology")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    device_names = {d["name"] for d in body["devices"]}
    transporter_names = {t["name"] for t in body["transporters"]}
    location_names = {loc["name"] for loc in body["locations"]}
    labware_names = {lt["name"] for lt in body["labware_templates"]}
    assert "shaker1" in device_names
    assert "robot1" in transporter_names
    assert {"pad1", "pad2"} <= location_names
    assert {"plate_96", "plate_96_b"} <= labware_names


@pytest.mark.asyncio
async def test_topology_source_returns_spec(client: AsyncClient) -> None:
    # The pre-loaded fixture injects the runtime directly (no mount), so the
    # spec on app.state is None and the source string is empty -- the route
    # still answers 200 with the source-projection shape.
    resp = await client.get("/topology", params={"source": "true"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "source" in body
    assert body["last_modified_sha"] is None


@pytest.mark.asyncio
async def test_runtime_status_built_when_loaded(client: AsyncClient) -> None:
    resp = await client.get("/runtime/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["built"] is True
    assert body["last_build_error"] is None


@pytest.mark.asyncio
async def test_runtime_status_not_built_when_empty(empty_client: AsyncClient) -> None:
    """The diagnostic route answers even with no system loaded (no 409)."""
    resp = await empty_client.get("/runtime/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["built"] is False


@pytest.mark.asyncio
async def test_ops_history_get_unknown_execution_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/operations/list-ops-history", json={"execution_id": "no-such-exec"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_ops_history_search_empty_body_returns_records_list(
    client: AsyncClient,
) -> None:
    resp = await client.post("/operations/search-ops-history", json={})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["records"], list)


@pytest.mark.asyncio
async def test_labware_journey_unknown_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/operations/get-labware-journey", json={"labware_id": "no-such-labware"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_clear_all_returns_cleared_ids(client: AsyncClient) -> None:
    resp = await client.post("/labware/runtime/clear-all", json={"force": True})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["cleared_labware_ids"], list)


@pytest.mark.asyncio
async def test_discharge_unknown_labware_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/labware/runtime/discharge",
        params={"labware_id": "no-such-labware"},
        json={"force": True},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_clear_submission_unknown_id_is_404(client: AsyncClient) -> None:
    """This used to assert a 200 with two empty lists, which only held because
    the facade answered an id it had never seen with an empty clear. An
    operator holding a stale id reads that as "the deck is already clear", so
    the route now maps it the way its discharge sibling above does. The
    success shape is pinned against a real submission in
    tests/operations/test_clear_typed_envelopes.py."""
    resp = await client.post(
        "/labware/runtime/clear-submission",
        params={"submission_id": "no-such-submission"},
        json={"force": True},
    )
    assert resp.status_code == 404, resp.text
