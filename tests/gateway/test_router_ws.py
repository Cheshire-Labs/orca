"""Behavioral tests for the /ws/devices router.

The router is built over the server's gateway with its handshake authenticator
passed in. These tests pin that delegation and what happens to the messages a
client sends after its hello.
"""

import asyncio
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect
import pytest

from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.gateway_protocol import (
    PROTOCOL_VERSION,
    AssignedDevice,
    ClientHello,
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
    DriverMode,
    MessageEnvelope,
    StatusMessage,
)
from orca.gateway.auth import IConnectionAuthenticator, NullConnectionAuthenticator
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.gateway.websocket.router import create_gateway_router
from tests.gateway_parts import GatewayParts, answer_assignment, gateway_parts


class _AlwaysReject:
    async def authenticate(self, api_key: str | None) -> bool:
        return False

    async def may_claim(self, api_key: str | None, client_id: str) -> bool:
        return False


class _RecordingAuthenticator:
    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def authenticate(self, api_key: str | None) -> bool:
        self.seen.append(api_key)
        return True

    async def may_claim(self, api_key: str | None, client_id: str) -> bool:
        return True


def _app(
    parts: GatewayParts, authenticator: IConnectionAuthenticator | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(create_gateway_router(
        parts.gateway, authenticator=authenticator or NullConnectionAuthenticator(),
    ))
    return app


@pytest.fixture
def parts() -> GatewayParts:
    return gateway_parts()


def _hello_envelope(version: str = PROTOCOL_VERSION) -> str:
    msg = ClientHello(client_id="lab_pc", site="boston", lab="molbio")
    payload = MessageEnvelope.wrap_hello(msg).model_dump()
    payload["payload"]["protocol_version"] = version
    return MessageEnvelope(**payload).model_dump_json()


def test_allow_all_default_accepts_keyless_connect(parts: GatewayParts) -> None:
    client = TestClient(_app(parts))
    with client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello_envelope())
        # No exception on send => handshake accepted under the allow-all default.
        ws.close()


def test_rejecting_authenticator_refuses_before_accept(parts: GatewayParts) -> None:
    client = TestClient(_app(parts, authenticator=_AlwaysReject()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/devices") as ws:
            ws.receive_text()


def test_injected_authenticator_is_consulted_with_header(parts: GatewayParts) -> None:
    auth = _RecordingAuthenticator()
    client = TestClient(_app(parts, authenticator=auth))
    with client.websocket_connect("/ws/devices", headers={"X-API-Key": "secret"}) as ws:
        ws.send_text(_hello_envelope())
        ws.close()
    assert auth.seen == ["secret"]


def test_protocol_version_mismatch_closes_after_accept(parts: GatewayParts) -> None:
    client = TestClient(_app(parts))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/devices") as ws:
            ws.send_text(_hello_envelope(version="9.9.9"))
            ws.receive_text()


def test_malformed_hello_closes_after_accept(parts: GatewayParts) -> None:
    """A hello with the right protocol version but an otherwise invalid body
    (here: ``site`` is not a string) must close the socket explicitly, not hang
    the client on its receive. Without the explicit close the ValidationError
    falls to the broad handler and the implicit close-on-return does not
    propagate."""
    msg = ClientHello(client_id="lab_pc", site="boston", lab="molbio")
    payload = MessageEnvelope.wrap_hello(msg).model_dump()
    payload["payload"]["site"] = ["not-a-string"]
    bad = MessageEnvelope(**payload).model_dump_json()

    client = TestClient(_app(parts))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/devices") as ws:
            ws.send_text(bad)
            ws.receive_text()


def _status_envelope(name: str, links: dict[DriverMode, DeviceLinkInfo]) -> str:
    msg = StatusMessage(
        devices={name: DeviceStatusInfo(status="ready", links=links)},
        timestamp=1.0,
    )
    return MessageEnvelope.wrap_status(msg).model_dump_json()


def _connect_with_device(parts: GatewayParts, ws: WebSocketTestSession, name: str) -> None:
    assigned = AssignedDevice(name=name, kind="transporter", setup=DeviceSetup(client="lab_pc"))
    asyncio.run(parts.assignments.publish([assigned]))
    ws.send_text(_hello_envelope())
    answer_assignment(ws, DeviceConnectInfo(name=name, type="transporter", interfaces=frozenset({"ITransporter"})))


def _await_reported(
    parts: GatewayParts, name: str, timeout: float = 10.0,
) -> DeviceSnapshot:
    """Wait for the server thread to apply a status message.

    The app runs on TestClient's own event loop, so the tracker's lock belongs
    to a different loop than the test's. `peek_snapshot` is the sync accessor
    for exactly that: no await, no second loop.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = parts.tracker.peek_snapshot(name)
        if snapshot is not None and snapshot.link_mode is not None:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"no status applied for {name!r} within {timeout}s")


def test_an_agents_status_report_reaches_the_registry(parts: GatewayParts) -> None:
    """The whole seam: what the device bridge says about its driver is what a
    reader sees.

    Without this the two halves can pass their own suites while the wire between
    them carries nothing, which is how the flag came to be read off a simulator.
    """
    client = TestClient(_app(parts))
    with client.websocket_connect("/ws/devices") as ws:
        _connect_with_device(parts, ws, "pf400_1")
        ws.send_text(_status_envelope(
            "pf400_1",
            {"LIVE": DeviceLinkInfo(is_connected=True, is_initialized=False)},
        ))

        snapshot = _await_reported(parts, "pf400_1")
        assert snapshot.is_connected is True
        assert snapshot.is_initialized is False
        assert snapshot.link_mode == "LIVE"
        ws.close()


def test_an_agents_status_report_reaches_the_connection_event_bus(parts: GatewayParts) -> None:
    """The other half of the same seam, and the one with teeth.

    A device bridge that restarted reports "not brought up" here and nowhere
    else; a subscriber that never hears it leaves orca's cached bring-up in
    place, and the next pick moves an arm nobody brought up.
    """
    seen: list[tuple[str, bool]] = []

    async def listener(device_name: str, reported: DeviceStatusInfo) -> None:
        seen.append((device_name, reported.observed_link.is_initialized))

    parts.events.subscribe_reported(listener)
    try:
        client = TestClient(_app(parts))
        with client.websocket_connect("/ws/devices") as ws:
            _connect_with_device(parts, ws, "pf400_3")
            ws.send_text(_status_envelope(
                "pf400_3",
                {"LIVE": DeviceLinkInfo(is_connected=True, is_initialized=False)},
            ))
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not seen:
                time.sleep(0.01)
            ws.close()
        assert seen == [("pf400_3", False)]
    finally:
        parts.events.unsubscribe_reported(listener)


def test_a_report_with_only_a_simulator_link_says_so_across_the_wire(parts: GatewayParts) -> None:
    """A SIM bench must not arrive looking like an instrument.

    The device bridge reports one link per driver it holds; the registry answers
    no run mode, so the mode it recorded is the only thing that keeps a reader
    from presenting a simulator's open link as the hardware's.
    """
    client = TestClient(_app(parts))
    with client.websocket_connect("/ws/devices") as ws:
        _connect_with_device(parts, ws, "pf400_2")
        ws.send_text(_status_envelope(
            "pf400_2",
            {
                "LIVE": DeviceLinkInfo(is_connected=False, is_initialized=False),
                "SIM": DeviceLinkInfo(is_connected=True, is_initialized=True),
            },
        ))

        snapshot = _await_reported(parts, "pf400_2")
        assert snapshot.is_connected is True
        assert snapshot.link_mode == "SIM"
        ws.close()
