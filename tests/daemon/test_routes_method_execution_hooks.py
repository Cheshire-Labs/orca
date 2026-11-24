"""`POST /api/method-executions` carries `event_hooks` and `deployment_profile` to the run."""
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import JsonValue

from orca.daemon.app import InjectedSystem, create_app
from tests.memory_activity import memory_daemon_system
from tests.standalone_method_hooks_world import ACTION_COMPLETED, METHOD, READ_DONE, Heard, a_built_reading_workflow


@dataclass
class _Daemon:
    client: AsyncClient
    injected: InjectedSystem
    heard: Heard


@pytest_asyncio.fixture
async def daemon() -> AsyncIterator[_Daemon]:
    heard = Heard()
    build = await a_built_reading_workflow(heard)
    injected = await memory_daemon_system(build.system)
    await injected.runtime.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(injected=injected)), base_url="http://daemon.test") as c:
            yield _Daemon(c, injected, heard)
    finally:
        await injected.runtime.shutdown(confirm=True)


async def _run(daemon: _Daemon, **extra: JsonValue) -> Heard:
    resp = await daemon.client.post("/api/method-executions", json={**METHOD, "run_mode": "SIM", **extra})
    assert resp.status_code == 201, resp.text
    ended = await daemon.injected.runtime.wait(resp.json()["execution_id"])
    assert ended.status.value == "completed", ended
    return daemon.heard


async def test_omitting_event_hooks_fires_every_handler(daemon: _Daemon) -> None:
    heard = await _run(daemon)
    assert heard.read_done == [READ_DONE] and heard.action_completed


async def test_an_empty_event_hooks_list_fires_none(daemon: _Daemon) -> None:
    assert await _run(daemon, event_hooks=[]) == Heard()


async def test_named_event_hooks_fire_only_their_handlers(daemon: _Daemon) -> None:
    heard = await _run(daemon, event_hooks=[ACTION_COMPLETED])
    assert heard.read_done == [] and heard.action_completed


async def test_an_event_hook_the_workflow_does_not_have_is_refused_as_invalid(daemon: _Daemon) -> None:
    resp = await daemon.client.post(
        "/api/method-executions", json={**METHOD, "run_mode": "SIM", "event_hooks": ["CUSTOM.nope.EMITTED"]},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "validation_error"
    assert READ_DONE in resp.json()["detail"]["message"]


async def test_the_deployment_profile_reaches_the_run(daemon: _Daemon) -> None:
    resp = await daemon.client.post(
        "/api/method-executions", json={**METHOD, "run_mode": "SIM", "deployment_profile": "no_such_profile"},
    )
    assert resp.status_code == 404, resp.text
    assert "no_such_profile" in resp.json()["detail"]["message"]
