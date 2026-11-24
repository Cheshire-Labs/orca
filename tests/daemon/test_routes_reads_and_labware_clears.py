"""Daemon route tests for the reads and labware clears the CLI calls.

Routes covered:

- GET  /methods/{name}
- GET  /workflows/{name}
- GET  /topology  (+ ?source=true)
- GET  /runtime/status
- GET  /ops-history
- GET  /labware-instances/{labware_id}/journey
- POST /submissions/{submission_id}/clear-labware, /labware-instances/{labware_id}/discharge,
  /labware-instances/clear-all
"""

from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_get_method_returns_summary(client: AsyncClient) -> None:
    # shake_method exists in both test workflows; disambiguate by workflow.
    resp = await client.get("/api/methods/shake_method", params={"workflow_name": "simple_workflow"})
    assert resp.status_code == 200, resp.text
    body = resp.json()["method"]
    assert body["name"] == "shake_method"
    assert body["workflow_name"] == "simple_workflow"
    assert "failure_policy" in body


@pytest.mark.asyncio
async def test_get_method_unknown_is_404(client: AsyncClient) -> None:
    resp = await client.get("/api/methods/nope")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_get_workflow_returns_summary(client: AsyncClient) -> None:
    resp = await client.get("/api/workflows/simple_workflow")
    assert resp.status_code == 200, resp.text
    body = resp.json()["workflow"]
    assert body["name"] == "simple_workflow"
    assert body["entry_thread_template_names"]  # non-empty entry-thread list


@pytest.mark.asyncio
async def test_topology_view_lists_topology_entries(client: AsyncClient) -> None:
    resp = await client.get("/api/topology")
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
async def test_a_topology_device_is_what_topology_py_declares(client: AsyncClient) -> None:
    """Live state is the device registry's answer; the topology lists declarations, transporters apart."""
    body = (await client.get("/api/topology")).json()
    shaker = next(d for d in body["devices"] if d["name"] == "shaker1")
    assert set(shaker) == {
        "name", "declared_kind", "declared_interfaces", "mounting_locations",
        "declared_interfaces_are_class_defaults",
    }
    assert "robot1" not in {d["name"] for d in body["devices"]}


@pytest.mark.asyncio
async def test_topology_source_without_a_topology_file_names_the_folder(client: AsyncClient) -> None:
    # The pre-loaded fixture injects a runtime into an empty package folder.
    resp = await client.get("/api/topology", params={"source": "true"})
    assert resp.status_code == 404, resp.text
    assert "no topology.py in" in resp.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_runtime_status_built_when_loaded(client: AsyncClient) -> None:
    resp = await client.get("/api/runtime/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["built"] is True
    assert body["last_build_error"] is None


@pytest.mark.asyncio
async def test_runtime_status_not_built_when_empty(empty_client: AsyncClient) -> None:
    """The diagnostic route answers even with no system loaded (no 409)."""
    resp = await empty_client.get("/api/runtime/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["built"] is False


@pytest.mark.asyncio
async def test_ops_history_search_for_an_unknown_execution_is_404(client: AsyncClient) -> None:
    resp = await client.get("/api/ops-history", params={"execution_id": "no-such-exec"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "execution_not_found"


@pytest.mark.asyncio
async def test_ops_history_search_empty_body_returns_records_list(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/ops-history")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["records"], list)


@pytest.mark.asyncio
async def test_labware_journey_unknown_is_404(client: AsyncClient) -> None:
    resp = await client.get("/api/labware-instances/no-such-labware/journey")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_clear_all_returns_cleared_ids(client: AsyncClient) -> None:
    resp = await client.post("/api/labware-instances/clear-all", json={"force": True})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["cleared"], list)


@pytest.mark.asyncio
async def test_discharge_unknown_labware_is_404(client: AsyncClient) -> None:
    resp = await client.post("/api/labware-instances/no-such-labware/discharge", json={"force": True})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_clear_submission_unknown_id_is_404(client: AsyncClient) -> None:
    """This used to assert a 200 with two empty lists, which only held because
    the facade answered an id it had never seen with an empty clear. An
    operator holding a stale id reads that as "the deck is already clear", so
    the route now maps it the way its discharge sibling above does. The
    success shape is pinned against a real submission in
    tests/operations/test_clear_typed_envelopes.py."""
    resp = await client.post("/api/submissions/no-such-submission/clear-labware", json={"force": True})
    assert resp.status_code == 404, resp.text
