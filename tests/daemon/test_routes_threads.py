"""Route tests for the thread read: ``GET /executions/{eid}/threads/{tid}``.

An execution's threads are listed on ``GET /executions/{eid}``.
"""

from httpx import AsyncClient


async def test_thread_detail_unknown_execution_returns_404(
    client: AsyncClient,
) -> None:
    """`GET /executions/{id}/threads/{tid}` with unknown ids must 404."""
    resp = await client.get("/api/executions/nope/threads/also-nope")
    assert resp.status_code == 404


async def test_thread_detail_carries_completed_methods_list(
    client: AsyncClient,
) -> None:
    """A submitted workflow's thread instance reports its completed methods
    as a JSON list of template names.

    The runtime intentionally does not predict future methods (generator state
    cannot be inspected), so the snapshot only exposes completed + current.
    This test verifies the shape a CLI / MCP caller can depend on.
    """
    import asyncio

    submit = await client.post(
        "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["execution_id"]

    # Workflow submission returns before thread registration completes;
    # yield briefly so the thread factory populates the registry before we query.
    for _ in range(30):
        detail = await client.get(f"/api/executions/{exec_id}")
        if detail.status_code == 200 and detail.json()["threads"]:
            break
        await asyncio.sleep(0.05)
    else:
        assert False, "thread never registered after submit"

    tid = detail.json()["threads"][0]["id"]

    detail_resp = await client.get(f"/api/executions/{exec_id}/threads/{tid}")
    assert detail_resp.status_code == 200
    body = detail_resp.json()
    assert "upcoming" not in body, (
        "upcoming field was removed; snapshots only report completed + current"
    )
    assert isinstance(body["completed_methods"], list), (
        f"completed_methods must be a list of template names; got "
        f"{type(body['completed_methods']).__name__}"
    )
