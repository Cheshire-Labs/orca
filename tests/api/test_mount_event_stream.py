"""The one live event route, as both servers mount it."""

import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.params import Depends
from fastapi.testclient import TestClient
from starlette.types import Message, Scope
from starlette.websockets import WebSocketDisconnect

from orca.api import EVENT_STREAM_PATH, IEventStream, WebSocketRefusal, mount_event_stream
from orca.daemon.app import create_app
from orca.api.event_feed import EventFeed
from orca.events.execution_context import WorkflowExecutionContext
from orca.events.runtime_event import RuntimeEvent
from tests.daemon.fixture_package import write_package


def _event(execution_id: str = "exec-1") -> RuntimeEvent:
    return RuntimeEvent(
        event_name="thread.t-1.STARTED",
        execution_id=execution_id,
        timestamp=1.0,
        entity_type="thread",
        entity_id="t-1",
        status="STARTED",
        context=WorkflowExecutionContext(execution_id=execution_id, workflow_name="wf"),
    )


class _Open:
    def dependencies(self) -> Sequence[Depends]:
        return ()

    async def refuse_websocket(self, websocket: WebSocket) -> WebSocketRefusal | None:
        return None


class _Refuses:
    def dependencies(self) -> Sequence[Depends]:
        return ()

    async def refuse_websocket(self, websocket: WebSocket) -> WebSocketRefusal | None:
        return WebSocketRefusal(code=1008, reason="Invalid API key")


def _app(stream: IEventStream | None, auth: _Open | _Refuses) -> FastAPI:
    router = APIRouter()
    mount_event_stream(router, lambda _: stream, auth)
    app = FastAPI()
    app.include_router(router)
    return app


def test_the_route_sends_each_event_as_its_json_text() -> None:
    sink = EventFeed()
    with TestClient(_app(sink, _Open())).websocket_connect(EVENT_STREAM_PATH) as ws:
        sink.on_event(_event())
        assert json.loads(ws.receive_text()) == json.loads(json.dumps(_event().to_dict()))


def test_the_execution_id_filter_reaches_the_stream() -> None:
    sink = EventFeed()
    client = TestClient(_app(sink, _Open()))
    with client.websocket_connect(f"{EVENT_STREAM_PATH}?execution_id=watched") as ws:
        sink.on_event(_event("other"))
        sink.on_event(_event("watched"))
        assert json.loads(ws.receive_text())["execution_id"] == "watched"


def test_a_refused_connection_closes_with_the_auth_answer_and_never_subscribes() -> None:
    sink = EventFeed()
    with pytest.raises(WebSocketDisconnect) as refused:
        with TestClient(_app(sink, _Refuses())).websocket_connect(EVENT_STREAM_PATH) as ws:
            ws.receive_text()
    assert (refused.value.code, refused.value.reason) == (1008, "Invalid API key")
    assert sink.subscriber_count == 0


def test_a_server_with_no_stream_yet_closes_1011() -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with TestClient(_app(None, _Open())).websocket_connect(EVENT_STREAM_PATH) as ws:
            ws.receive_text()
    assert closed.value.code == 1011


async def test_a_client_that_leaves_a_quiet_feed_ends_the_handler() -> None:
    """No event ever arrives here, so only the client's disconnect can end it.

    uvicorn waits for this handler at shutdown, so a handler that never ends
    would hang `orca` shutdown.
    """
    sink = EventFeed()
    inbound = iter([
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1000},
    ])

    async def receive() -> Message:
        return next(inbound)

    async def send(_message: Message) -> None:
        return None

    scope: Scope = {
        "type": "websocket",
        "path": EVENT_STREAM_PATH,
        "raw_path": EVENT_STREAM_PATH.encode(),
        "root_path": "",
        "scheme": "ws",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    await asyncio.wait_for(_app(sink, _Open())(scope, receive, send), timeout=5.0)
    assert sink.subscriber_count == 0


def test_the_daemon_serves_the_route_and_its_mounted_runtimes_feed_it(tmp_path: Path) -> None:
    write_package(tmp_path)
    with TestClient(create_app(package_root=tmp_path)) as client:
        with client.websocket_connect(EVENT_STREAM_PATH) as ws:
            mounted = client.post("/api/runtime/mount-package", json={})
            assert mounted.status_code == 200, mounted.text
            run = client.post(
                "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"},
            )
            assert run.status_code == 200, run.text
            frame = json.loads(ws.receive_text())
            # Bringing the devices up happens before the run exists, so it is recorded as the system's.
            while frame["execution_id"] == "_system":
                frame = json.loads(ws.receive_text())
            # The package teaches no route, so the run pauses on its first move; end it so the runtime can unload.
            stop_path = f"/api/executions/{run.json()['execution_id']}/stop"
            client.post(stop_path, json={})
            client.post(stop_path, json={"confirm": True})
            while client.post("/api/runtime/unload", json={}).status_code == 409:
                time.sleep(0.05)

    assert frame["execution_id"] == run.json()["execution_id"]


def test_a_web_page_from_another_origin_cannot_read_the_daemons_feed(tmp_path: Path) -> None:
    client = TestClient(create_app(package_root=tmp_path))
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(EVENT_STREAM_PATH, headers={"origin": "https://evil.example"}):
            pass
    assert refused.value.code == 1008


def test_a_page_served_from_this_machine_and_a_client_with_no_origin_connect(tmp_path: Path) -> None:
    client = TestClient(create_app(package_root=tmp_path))
    for headers in ({"origin": "http://localhost:5173"}, {"origin": "http://127.0.0.1:8000"}, {}):
        with client.websocket_connect(EVENT_STREAM_PATH, headers=headers):
            pass
