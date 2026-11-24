"""A daemon mounting a package reads its `system_config.json` and `global_variables.json`, as `orca start` does."""
from httpx import AsyncClient

from tests.daemon.fixture_package import EVENT_WAIT_WORKFLOW, write_package
from tests.test_helpers import wait_until


async def _thread_statuses(client: AsyncClient, execution_id: str) -> list[str]:
    resp = await client.get(f"/api/executions/{execution_id}")
    assert resp.status_code == 200, resp.text
    return [t["status"] for t in resp.json()["threads"]]


async def test_a_mounted_package_waits_only_as_long_as_its_system_config_says(empty_client: AsyncClient) -> None:
    root = write_package(workflows=EVENT_WAIT_WORKFLOW)
    (root / "system_config.json").write_text('{"coordination": {"event_timeout": 1.0}}', encoding="utf-8")
    assert (await empty_client.post("/api/runtime/mount-package", json={})).status_code == 200
    try:
        submitted = await empty_client.post(
            "/api/executions", json={"workflow_name": "waits_for_an_event", "run_mode": "CHECK_RUN"},
        )
        assert submitted.status_code in (200, 201, 202), submitted.text
        execution_id = submitted.json()["execution_id"]

        seen: list[list[str]] = []

        async def paused() -> bool:
            seen.append(await _thread_statuses(empty_client, execution_id))
            return seen[-1] == ["PAUSED"]

        try:
            await wait_until(paused, timeout=20.0)
        except TimeoutError as timed_out:
            raise AssertionError(f"the event wait never timed out; last thread statuses {seen[-1:]}") from timed_out
        stop = {"confirm": True, "reason": "test over"}
        assert (await empty_client.post(f"/api/executions/{execution_id}/stop", json=stop)).status_code == 200
    finally:
        await empty_client.post("/api/runtime/unload")


async def test_a_mounted_package_declares_the_globals_in_its_file(empty_client: AsyncClient) -> None:
    root = write_package()
    (root / "global_variables.json").write_text(
        '{"plate_count": {"type": "int", "default": 1, "min": 1, "max": 4}}', encoding="utf-8",
    )
    assert (await empty_client.post("/api/runtime/mount-package", json={})).status_code == 200
    try:
        read = await empty_client.get("/api/variables/global/plate_count")
        assert read.status_code == 200, read.text
        assert (read.json()["value"], read.json()["source"]) == (1, "global-default")
        refused = await empty_client.put("/api/variables/global/plate_count", json={"value": 9})
        assert refused.status_code == 422, refused.text
    finally:
        await empty_client.post("/api/runtime/unload")
