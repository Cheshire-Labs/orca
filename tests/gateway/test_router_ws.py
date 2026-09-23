"""Behavioral tests for the relocated /ws/devices router.

The router is the one production file that was refactored (not a verbatim move):
handshake auth is delegated to an injected IConnectionAuthenticator read off
app.state, and the runtime is read off app.state.system_runtime. These tests
pin that delegation behavior.
"""

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from cheshire_drivers.gateway_protocol import (
    PROTOCOL_VERSION,
    ConnectMessage,
    DeviceConnectInfo,
    DeviceLinkInfo,
    DeviceStatusInfo,
    DriverMode,
    MessageEnvelope,
    StatusMessage,
)
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.gateway.registry import device_connection_tracker
from orca.gateway.websocket.connection_events import connection_events
from orca.gateway.websocket.manager import connection_manager
from orca.gateway.websocket.router import router


class _AlwaysReject:
    async def authenticate(self, api_key: str | None) -> bool:
        return False


class _RecordingAuthenticator:
    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def authenticate(self, api_key: str | None) -> bool:
        self.seen.append(api_key)
        return True


def _app(authenticator: object | None = None, system_runtime: object | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.system_runtime = system_runtime
    if authenticator is not None:
        app.state.connection_authenticator = authenticator
    return app


@pytest.fixture(autouse=True)
def _clean_singletons():
    """The connection manager + tracker are process singletons; isolate tests."""
    yield
    connection_manager._connections.clear()
    device_connection_tracker._clients.clear()
    device_connection_tracker._devices.clear()


def _connect_envelope(site: str = "boston", lab: str = "molbio", version: str = PROTOCOL_VERSION) -> str:
    msg = ConnectMessage(site=site, lab=lab, devices=[])
    env = MessageEnvelope.wrap_connect(msg)
    payload = env.model_dump()
    payload["payload"]["protocol_version"] = version
    return MessageEnvelope(**payload).model_dump_json()


def test_allow_all_default_accepts_keyless_connect() -> None:
    client = TestClient(_app())
    with client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_connect_envelope())
        # No exception on send => handshake accepted under the allow-all default.
        ws.close()


def test_rejecting_authenticator_refuses_before_accept() -> None:
    client = TestClient(_app(authenticator=_AlwaysReject()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/devices") as ws:
            ws.receive_text()


def test_injected_authenticator_is_consulted_with_header() -> None:
    auth = _RecordingAuthenticator()
    client = TestClient(_app(authenticator=auth))
    with client.websocket_connect("/ws/devices", headers={"X-API-Key": "secret"}) as ws:
        ws.send_text(_connect_envelope())
        ws.close()
    assert auth.seen == ["secret"]


def test_protocol_version_mismatch_closes_after_accept() -> None:
    client = TestClient(_app())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/devices") as ws:
            ws.send_text(_connect_envelope(version="9.9.9"))
            ws.receive_text()


def test_malformed_connect_closes_after_accept() -> None:
    """A connect with the right protocol version but an otherwise invalid body
    (here: ``devices`` is not a list) must close the socket explicitly, not hang
    the client on its receive. Without the explicit close the ValidationError
    falls to the broad handler and the implicit close-on-return does not
    propagate."""
    msg = ConnectMessage(site="boston", lab="molbio", devices=[])
    payload = MessageEnvelope.wrap_connect(msg).model_dump()
    payload["payload"]["devices"] = "not-a-list"
    bad = MessageEnvelope(**payload).model_dump_json()

    client = TestClient(_app())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/devices") as ws:
            ws.send_text(bad)
            ws.receive_text()


@pytest.mark.asyncio
async def test_a_second_lab_claiming_a_connected_device_is_refused() -> None:
    """One instrument, one connection. If another device bridge is already
    holding `pf400_1`, a newcomer advertising the same name is wired to the same
    arm, so it is closed at the handshake rather than quietly taking over the
    routing.

    The incumbent must survive: it keeps the device and its own socket.
    """
    incumbent = DeviceConnectInfo.model_validate(
        {"type": "transporter", "name": "pf400_1", "interfaces": ["ITransporter"]}
    )
    await device_connection_tracker.register_client(
        client_id="boston-molbio-client", site="boston", lab="molbio",
        workcell=None, devices=[incumbent],
    )
    try:
        msg = ConnectMessage(site="cambridge", lab="cellculture", devices=[incumbent])
        envelope = MessageEnvelope.wrap_connect(msg).model_dump_json()

        client = TestClient(_app())
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/devices") as ws:
                ws.send_text(envelope)
                ws.receive_text()

        assert await device_connection_tracker.get_client_for_device(
            "pf400_1"
        ) == "boston-molbio-client"
        refused = await connection_manager.get_connection("cambridge-cellculture-client")
        assert refused is None, "a refused client must not hold a connection"
    finally:
        await device_connection_tracker.unregister_client("boston-molbio-client")


def _status_envelope(name: str, links: dict[DriverMode, DeviceLinkInfo]) -> str:
    msg = StatusMessage(
        devices={name: DeviceStatusInfo(status="ready", links=links)},
        timestamp=1.0,
    )
    return MessageEnvelope.wrap_status(msg).model_dump_json()


def _connect_with_device_envelope(name: str) -> str:
    msg = ConnectMessage(
        site="boston",
        lab="molbio",
        devices=[
            DeviceConnectInfo(
                name=name, type="transporter", interfaces=frozenset({"ITransporter"}),
            )
        ],
    )
    return MessageEnvelope.wrap_connect(msg).model_dump_json()


def _await_reported(name: str, timeout: float = 10.0) -> DeviceSnapshot:
    """Wait for the server thread to apply a status message.

    The app runs on TestClient's own event loop, so the tracker's lock belongs
    to a different loop than the test's. `peek_snapshot` is the sync accessor
    for exactly that: no await, no second loop.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = device_connection_tracker.peek_snapshot(name)
        if snapshot is not None and snapshot.link_mode is not None:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"no status applied for {name!r} within {timeout}s")


def test_an_agents_status_report_reaches_the_registry() -> None:
    """The whole seam: what the device bridge says about its driver is what a
    reader sees.

    Without this the two halves can pass their own suites while the wire between
    them carries nothing, which is how the flag came to be read off a simulator.
    """
    client = TestClient(_app())
    with client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_connect_with_device_envelope("pf400_1"))
        ws.send_text(_status_envelope(
            "pf400_1",
            {"LIVE": DeviceLinkInfo(is_connected=True, is_initialized=False)},
        ))

        snapshot = _await_reported("pf400_1")
        assert snapshot.is_connected is True
        assert snapshot.is_initialized is False
        assert snapshot.link_mode == "LIVE"
        ws.close()


def test_an_agents_status_report_reaches_the_connection_event_bus() -> None:
    """The other half of the same seam, and the one with teeth.

    A device bridge that restarted reports "not brought up" here and nowhere
    else; a subscriber that never hears it leaves orca's cached bring-up in
    place, and the next pick moves an arm nobody brought up.
    """
    seen: list[tuple[str, bool]] = []

    async def listener(device_name: str, reported: DeviceStatusInfo) -> None:
        seen.append((device_name, reported.observed_link.is_initialized))

    connection_events.subscribe_reported(listener)
    try:
        client = TestClient(_app())
        with client.websocket_connect("/ws/devices") as ws:
            ws.send_text(_connect_with_device_envelope("pf400_3"))
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
        connection_events.unsubscribe_reported(listener)


def test_a_report_with_only_a_simulator_link_says_so_across_the_wire() -> None:
    """A DEVICE_SIM bench must not arrive looking like an instrument.

    The device bridge reports one link per driver it holds; the registry answers
    no run mode, so the mode it recorded is the only thing that keeps a reader
    from presenting a simulator's open link as the hardware's.
    """
    client = TestClient(_app())
    with client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_connect_with_device_envelope("pf400_2"))
        ws.send_text(_status_envelope(
            "pf400_2",
            {
                "LIVE": DeviceLinkInfo(is_connected=False, is_initialized=False),
                "DEVICE_SIM": DeviceLinkInfo(is_connected=True, is_initialized=True),
            },
        ))

        snapshot = _await_reported("pf400_2")
        assert snapshot.is_connected is True
        assert snapshot.link_mode == "DEVICE_SIM"
        ws.close()
