"""GET /events reads what the daemon stored through the `orca.events` logger.

The live feed is covered by tests/api/test_mount_event_stream.py.
"""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import JsonValue

from orca.daemon.app import InjectedSystem, create_app
from orca.daemon.daemon_state import DaemonState
from orca.events.execution_context import HostedEventContext, WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from orca.runtime.system_runtime import SystemRuntime
from tests.daemon.started_daemon import started_daemon


def _event(name: str, execution_id: str = "exec-test") -> RuntimeEvent:
    entity_type, entity_id, status = name.split(".")
    return RuntimeEvent(
        event_name=name,
        execution_id=execution_id,
        timestamp=100.0,
        entity_type=entity_type,
        entity_id=entity_id,
        status=status,
        context=WorkflowExecutionContext(execution_id=execution_id, workflow_name="w"),
    )


def _emit(app: FastAPI, event: RuntimeEvent) -> None:
    daemon: DaemonState = app.state.daemon
    daemon.events.emit(event)


async def _read_until(client: AsyncClient, count: int, query: str = "") -> list[dict[str, JsonValue]]:
    # The writer stores off the logging thread, so a just-logged event lands a moment later.
    body: list[dict[str, JsonValue]] = []
    for _ in range(1000):
        body = (await client.get(f"/api/events{query}")).json()
        if len(body) >= count:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"fewer than {count} events stored: {body}")


async def test_an_event_the_runtime_emits_is_stored_and_listed(
    injected: InjectedSystem, runtime: SystemRuntime,
) -> None:
    app = create_app(injected=injected)
    async with started_daemon(app) as client:
        runtime._system_event_bus.emit(_event("TEST.0.RUNNING"))

        body = await _read_until(client, 1)

    assert [(e["event_name"], e["execution_id"]) for e in body] == [("TEST.0.RUNNING", "exec-test")]
    assert {"cursor", "status", "context", "timestamp"} <= set(body[0])


async def test_events_are_still_listed_after_the_daemon_restarts(tmp_path: Path) -> None:
    first = create_app(package_root=tmp_path)
    async with started_daemon(first) as client:
        _emit(first, _event("THREAD.t1.RUNNING", "exec-1"))
        _emit(first, _event("THREAD.t2.RUNNING", "exec-2"))
        stored = await _read_until(client, 2)

    async with started_daemon(create_app(package_root=tmp_path)) as client:
        after_restart = (await client.get("/api/events")).json()
        one_run = (await client.get("/api/events?execution_id=exec-2")).json()
        tail = (await client.get("/api/events?tail=true&limit=1")).json()

    assert after_restart == stored
    assert [e["event_name"] for e in one_run] == ["THREAD.t2.RUNNING"]
    assert [e["event_name"] for e in tail] == ["THREAD.t2.RUNNING"]


async def test_command_and_labware_events_are_stored_and_a_hosted_event_is_not(tmp_path: Path) -> None:
    """Every event orca-core sends is stored; a hosted server's own event is its to keep."""
    app = create_app(package_root=tmp_path)
    async with started_daemon(app) as client:
        _emit(app, RuntimeEvent(
            event_name="hosted.state", execution_id="_system", timestamp=0.0, entity_type="HOSTED",
            entity_id="pf400_1", status="sample", context=HostedEventContext.model_validate({"device_name": "pf400_1"}),
        ))
        for name in ("COMMAND.shaker_1.COMPLETED", "LABWARE.lw-1.MOVED", "THREAD.t1.RUNNING"):
            _emit(app, _event(name, "_system"))
        stored = await _read_until(client, 3)

    assert [e["event_name"] for e in stored] == ["COMMAND.shaker_1.COMPLETED", "LABWARE.lw-1.MOVED", "THREAD.t1.RUNNING"]
