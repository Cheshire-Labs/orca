"""POST /api/runtime/reload rebuilds the daemon's package folder from the files on disk now.

Both servers serve the same reload Operation from the shared catalog.
"""

import asyncio

import pytest
from httpx import AsyncClient

from tests.daemon.fixture_package import BROKEN, SIMPLE_WORKFLOW, SLOW_WORKFLOW, write_package

pytestmark = pytest.mark.positions_bound_by_the_build

_UNKNOWN_TEACHPOINT = f"{BROKEN}:build_topology_with_an_unknown_teachpoint"


async def _workflow_names(client: AsyncClient) -> list[str]:
    catalog = await client.get("/api/workflows")
    assert catalog.status_code == 200, catalog.text
    return sorted(w["name"] for w in catalog.json()["workflows"])


async def _audited_reasons(client: AsyncClient) -> list[str]:
    # The audit writer saves rows on its own thread.
    for _ in range(50):
        rows = (await client.get("/api/audit", params={"action_name": "runtime.reload"})).json()
        if rows:
            return [row["reason"] for row in rows]
        await asyncio.sleep(0.02)
    return []


async def test_a_reload_builds_a_workflow_added_on_disk_since_the_mount(empty_client: AsyncClient) -> None:
    write_package(workflows=SIMPLE_WORKFLOW)
    assert (await empty_client.post("/api/runtime/mount-package", json={})).status_code == 200
    write_package(workflows={**SIMPLE_WORKFLOW, **SLOW_WORKFLOW})

    resp = await empty_client.post("/api/runtime/reload", json={"reason": "added a workflow"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["registered"] == ["simple_workflow", "slow_window_workflow"]
    assert body["head_sha"] is None
    assert await _workflow_names(empty_client) == ["simple_workflow", "slow_window_workflow"]
    assert await _audited_reasons(empty_client) == ["added a workflow"]
    await empty_client.post("/api/runtime/unload")


async def test_a_reload_is_refused_while_a_run_is_active(empty_client: AsyncClient) -> None:
    write_package(workflows=SLOW_WORKFLOW)
    await empty_client.post("/api/runtime/mount-package", json={})
    run = await empty_client.post(
        "/api/executions", json={"workflow_name": "slow_window_workflow", "run_mode": "CHECK_RUN"},
    )
    assert run.status_code == 200, run.text

    resp = await empty_client.post("/api/runtime/reload", json={})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "active_executions_exist"


async def test_a_reload_of_a_broken_package_leaves_no_runtime_and_says_why(empty_client: AsyncClient) -> None:
    write_package(workflows=SIMPLE_WORKFLOW)
    assert (await empty_client.post("/api/runtime/mount-package", json={})).status_code == 200
    write_package(topology=_UNKNOWN_TEACHPOINT, workflows={})

    resp = await empty_client.post("/api/runtime/reload", json={})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "rebuild_failed"
    assert "'nowhere'" in resp.json()["detail"]["message"]
    listed = await empty_client.get("/api/workflows")
    assert listed.status_code == 503
    assert listed.json()["detail"]["code"] == "runtime_not_ready"
    status = (await empty_client.get("/api/runtime/status")).json()
    assert status["built"] is False
    assert status["last_build_error"]["type"] == "ValueError"


async def test_after_a_reload_of_a_topology_with_a_syntax_error_the_blocker_names_the_build_error(
    empty_client: AsyncClient,
) -> None:
    root = write_package(workflows=SIMPLE_WORKFLOW)
    assert (await empty_client.post("/api/runtime/mount-package", json={})).status_code == 200
    (root / "topology.py").write_text("def build_topology(:", encoding="utf-8")

    assert (await empty_client.post("/api/runtime/reload", json={})).status_code == 400
    status = (await empty_client.get("/api/runtime/status")).json()

    [blocker] = status["blockers"]
    assert blocker["kind"] == "RUNTIME_NOT_BUILT"
    assert "SyntaxError" in blocker["headline"]
    assert blocker["detail"] == status["last_build_error"]["hint"]
    assert [step["cli"] for remedy in blocker["remedies"] for step in remedy["steps"]] == ["orca topology mount"]
