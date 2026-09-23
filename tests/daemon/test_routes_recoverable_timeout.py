"""Daemon recoverable-timeout decision routes resolve a parked device call.

Drives a real dispatch through the runtime's coordinator until it times out and
declares an incident, then resolves it via the REST routes (extend / abort /
mark_complete). Parity with a hosted deployment's /api/incidents/{id}/recoverable_timeout/*.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.events.intervention import INCIDENT_ENTITY
from orca.events.runtime_event import RuntimeEvent
from orca.resource_models.device_error import CommandTimeoutAbortedError
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def rt_client() -> AsyncIterator[tuple[SystemRuntime, AsyncClient]]:
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system)
    await rt.start()
    app = create_app(initial_system_runtime=rt)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://daemon.test",
        ) as c:
            yield rt, c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


class _IncidentSink:
    """Signals the first RECOVERABLE_TIMEOUT incident event, event-driven.

    The runtime emits an INCIDENT RuntimeEvent the instant an incident is
    recorded; subscribing to it (as any real sink / UI does) lets the test await
    the parked dispatch without polling.
    """

    def __init__(self) -> None:
        self.parked: asyncio.Event = asyncio.Event()
        self.incident_id: str | None = None

    def on_event(self, event: RuntimeEvent) -> None:
        # Inline set() is safe only because RECOVERABLE_TIMEOUT is recorded
        # on-loop; an off-loop category would need call_soon_threadsafe.
        if (
            self.incident_id is None
            and event.entity_type == INCIDENT_ENTITY
            and event.status == IncidentCategory.RECOVERABLE_TIMEOUT.value
        ):
            self.incident_id = event.entity_id
            self.parked.set()


async def _park_dispatch(rt: SystemRuntime, coro_factory) -> tuple[asyncio.Task, str]:
    """Start a dispatch that times out fast; return (task, incident_id).

    Awaits the incident event (delivered the instant it is recorded) instead of
    polling ``incidents.list()`` in a 20ms loop. That hot DB poll churned the
    event loop enough to destabilise the coordinator's own timeout under parallel
    load; a lab runtime works at second-to-hour timescales, so a tight poll is the
    wrong synchronisation primitive here.
    """
    sink = _IncidentSink()
    rt.register_sink(sink)
    task = asyncio.ensure_future(
        rt.recoverable_timeout_coordinator.run_with_timeout(
            "exec-test", "dev", "cmd", max_seconds=0.05, coro_factory=coro_factory,
        )
    )
    try:
        await asyncio.wait_for(sink.parked.wait(), timeout=15.0)
    except (asyncio.TimeoutError, TimeoutError):
        task.cancel()
        raise AssertionError("recoverable-timeout incident not declared") from None
    assert sink.incident_id is not None
    return task, sink.incident_id


async def test_extend_route_lets_the_call_complete(rt_client) -> None:
    rt, client = rt_client

    async def slow() -> str:
        # Stay in-flight until the recoverable-timeout incident is declared,
        # then finish so extend resolves a still-parked call.
        sink = _IncidentSink()
        rt.register_sink(sink)
        await asyncio.wait_for(sink.parked.wait(), timeout=15.0)
        return "done"

    task, incident_id = await _park_dispatch(rt, slow)
    resp = await client.post(
        f"/incidents/{incident_id}/recoverable_timeout/extend",
        json={"additional_seconds": 5.0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "extend"
    assert await task == "done"


@pytest.mark.parametrize("additional_seconds", [0, -1])
async def test_extend_route_rejects_non_positive_seconds(
    rt_client, additional_seconds: int,
) -> None:
    rt, client = rt_client

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task, incident_id = await _park_dispatch(rt, stuck)
    try:
        resp = await client.post(
            f"/incidents/{incident_id}/recoverable_timeout/extend",
            json={"additional_seconds": additional_seconds},
        )
        assert resp.status_code == 422, resp.text
    finally:
        rt.recoverable_timeout_abort(incident_id, "cleanup", "test teardown")
        with pytest.raises(CommandTimeoutAbortedError):
            await task


async def test_abort_route_fails_the_call(rt_client) -> None:
    rt, client = rt_client

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task, incident_id = await _park_dispatch(rt, stuck)
    resp = await client.post(
        f"/incidents/{incident_id}/recoverable_timeout/abort",
        json={"operator_name": "alice", "reason": "hung"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "abort"
    with pytest.raises(CommandTimeoutAbortedError):
        await task


async def test_mark_complete_route_synthesizes_success(rt_client) -> None:
    rt, client = rt_client

    async def stuck() -> str:
        await asyncio.sleep(100)
        return "never"

    task, incident_id = await _park_dispatch(rt, stuck)
    resp = await client.post(
        f"/incidents/{incident_id}/recoverable_timeout/mark_complete",
        json={"operator_name": "bob", "reason": "did it manually"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "mark_complete"
    assert await task is None


async def test_unknown_incident_is_404(rt_client) -> None:
    _, client = rt_client
    resp = await client.post(
        "/incidents/does-not-exist/recoverable_timeout/extend",
        json={"additional_seconds": 1.0},
    )
    assert resp.status_code == 404, resp.text
