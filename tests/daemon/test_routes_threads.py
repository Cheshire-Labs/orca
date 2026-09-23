"""Route tests for thread read endpoints (list/detail). Error-path focused.

Thread MUTATION coverage (pause/resume/skip/abort/insert/recover/spawn)
moved to the unified Operations surface on the daemon under
``/operations/<op>`` -- see ``tests/operations/test_daemon_operation_parity.py``.
The read endpoints here (``GET /executions/{eid}/threads`` and
``GET /executions/{eid}/threads/{tid}``) stay on the legacy decorator
surface; this file pins their error mappings.
"""

from httpx import AsyncClient


async def test_thread_detail_unknown_execution_returns_404(
    client: AsyncClient,
) -> None:
    """`GET /executions/{id}/threads/{tid}` with unknown ids must 404."""
    resp = await client.get("/executions/nope/threads/also-nope")
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
        "/executions", json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert submit.status_code == 200
    exec_id = submit.json()["id"]

    # Workflow submission returns before thread registration completes;
    # yield briefly so the thread factory populates the registry before we query.
    for _ in range(30):
        threads_resp = await client.get(f"/executions/{exec_id}/threads")
        if threads_resp.status_code == 200 and threads_resp.json():
            break
        await asyncio.sleep(0.05)
    else:
        assert False, "thread never registered after submit"

    threads = threads_resp.json()
    tid = threads[0]["id"]

    detail_resp = await client.get(f"/executions/{exec_id}/threads/{tid}")
    assert detail_resp.status_code == 200
    body = detail_resp.json()
    assert "upcoming" not in body, (
        "upcoming field was removed; snapshots only report completed + current"
    )
    assert isinstance(body["completed_methods"], list), (
        f"completed_methods must be a list of template names; got "
        f"{type(body['completed_methods']).__name__}"
    )
