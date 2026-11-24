"""A finished run still reads after every labware is cleared: its threads report their location as unknown."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli import execution as execution_cli
from orca.cli.client import OrcaClient
from orca.daemon.app import create_app
from tests.memory_activity import memory_daemon_system
from tests.runtime.manual_place_fixtures import _live_connection_source
from tests.test_system_runtime import _build_simple_system

runner = CliRunner()


class _Thread(BaseModel):
    id: str
    status: str
    current_location: str | None
    completed_method_count: int


class _Detail(BaseModel):
    status: str
    threads: list[_Thread]


@pytest_asyncio.fixture
async def daemon() -> AsyncIterator[AsyncClient]:
    """A daemon whose devices are connected, so it admits a sim run."""
    system, _ = await _build_simple_system(leave_in_place=True)
    injected = await memory_daemon_system(system, connection_source=_live_connection_source())
    await injected.runtime.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(injected=injected)), base_url="http://daemon.test",
        ) as client:
            yield client
    finally:
        await injected.runtime.shutdown(confirm=True)


class _CliClient(OrcaClient):
    """The CLI's own client, with each request carried to the in-process daemon on the test's loop."""

    def __init__(self, daemon: AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__("http://daemon.test", timeout=30.0)
        self._daemon = daemon
        self._loop = loop

    def _forward(self, request: httpx.Request) -> httpx.Response:
        sent = asyncio.run_coroutine_threadsafe(
            self._daemon.request(
                request.method, request.url.raw_path.decode(), content=request.content, headers=request.headers,
            ),
            self._loop,
        )
        answer = sent.result(timeout=30.0)
        return httpx.Response(answer.status_code, content=answer.content, headers=answer.headers)

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self._forward), base_url=self._base_url, timeout=self._timeout,
        )


async def _finished_run(daemon: AsyncClient) -> str:
    submitted = await daemon.post(
        "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "SIM", "sim_speedup": 1000.0},
    )
    assert submitted.status_code == 200, submitted.text
    execution_id: str = submitted.json()["execution_id"]
    for _ in range(600):
        if (await _detail(daemon, execution_id)).status == "completed":
            return execution_id
        await asyncio.sleep(0.05)
    pytest.fail("the run did not complete")


async def _detail(daemon: AsyncClient, execution_id: str) -> _Detail:
    answer = await daemon.get(f"/api/executions/{execution_id}")
    assert answer.status_code == 200, answer.text
    return _Detail.model_validate_json(answer.content)


@pytest.mark.timeout(120)
async def test_a_finished_runs_detail_threads_and_thread_detail_read_after_a_clear_all(
    daemon: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = await _finished_run(daemon)
    before = (await _detail(daemon, execution_id)).threads
    assert before
    assert all(thread.current_location for thread in before), "each plate has a position while it is on record"

    cleared = await daemon.post("/api/labware-instances/clear-all", json={"reason": "the deck was emptied"})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared"]

    unknown = [None] * len(before)
    after = (await _detail(daemon, execution_id)).threads
    assert [thread.current_location for thread in after] == unknown
    # The stored record keeps no method count, so this is still the runtime's own answer.
    assert [thread.completed_method_count for thread in after] == [thread.completed_method_count for thread in before]
    assert all(thread.completed_method_count > 0 for thread in after)

    cli_client = _CliClient(daemon, asyncio.get_running_loop())
    monkeypatch.setattr(execution_cli, "get_client", lambda: cli_client)
    shown = await asyncio.to_thread(runner.invoke, app, ["execution", "thread", "detail", execution_id, before[0].id])
    assert shown.exit_code == 0, shown.output
    assert "unknown" in shown.output


@pytest.mark.timeout(120)
async def test_the_execution_list_reads_after_a_clear_all(daemon: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The list reads every run's threads, so one thread with no position must not fail the whole list."""
    execution_id = await _finished_run(daemon)
    cleared = await daemon.post("/api/labware-instances/clear-all", json={"reason": "the deck was emptied"})
    assert cleared.status_code == 200, cleared.text

    listed = await daemon.get("/api/executions")
    assert listed.status_code == 200, listed.text
    assert execution_id in [run["id"] for run in listed.json()["executions"]]

    cli_client = _CliClient(daemon, asyncio.get_running_loop())
    monkeypatch.setattr(execution_cli, "get_client", lambda: cli_client)
    shown = await asyncio.to_thread(runner.invoke, app, ["execution", "list"])
    assert shown.exit_code == 0, shown.output
    assert execution_id[:8] in shown.output


@pytest.mark.timeout(120)
async def test_the_execution_list_reads_while_a_live_runs_labware_was_cleared_by_force(daemon: AsyncClient) -> None:
    """A forced clear-all under a live run leaves its thread with no position. The list still answers, and so
    does the run's own detail."""
    submitted = await daemon.post(
        "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "SIM", "sim_speedup": 0.1},
    )
    assert submitted.status_code == 200, submitted.text
    execution_id: str = submitted.json()["execution_id"]
    for _ in range(200):
        threads = (await _detail(daemon, execution_id)).threads
        if threads and all(thread.current_location for thread in threads):
            break
        await asyncio.sleep(0.05)
    cleared = await daemon.post(
        "/api/labware-instances/clear-all", json={"reason": "the deck was emptied", "force": True},
    )
    assert cleared.status_code == 200, cleared.text

    listed = await daemon.get("/api/executions")

    assert listed.status_code == 200, listed.text
    assert execution_id in [run["id"] for run in listed.json()["executions"]]
    detail = await daemon.get(f"/api/executions/{execution_id}")
    assert detail.status_code == 200, detail.text
