"""The orca daemon serves the device-bridge gateway websocket route."""

import time

from fastapi.testclient import TestClient

from cheshire_drivers.gateway_protocol import ClientHello, DeviceConnectInfo, MessageEnvelope
from orca.daemon.app import create_app
from tests.gateway_parts import answer_assignment


def test_daemon_mounts_ws_devices_route() -> None:
    app = create_app()
    ws_paths = {
        p
        for route in app.routes
        if (p := getattr(route, "path", None)) is not None
    }
    assert "/ws/devices" in ws_paths


def test_ws_devices_allows_a_keyless_device_bridge() -> None:
    """A daemon trusts its LAN: a device bridge that sends no key is accepted
    and its devices reach the daemon's own roster."""
    app = create_app()
    hello = ClientHello(client_id="lab_pc", site="boston", lab="molbio")
    built = DeviceConnectInfo(name="shaker_1", type="shaker", interfaces=frozenset({"IShaker"}))
    gateway = app.state.daemon.connections.gateway
    tracker = gateway.tracker
    with TestClient(app) as client, client.websocket_connect("/ws/devices") as ws:
        assert client.portal is not None
        client.portal.call(gateway.assignments.publish, [])
        ws.send_text(MessageEnvelope.wrap_hello(hello).model_dump_json())
        answer_assignment(ws, built)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and tracker.peek_snapshot("shaker_1") is None:
            time.sleep(0.01)
        assert tracker.peek_snapshot("shaker_1") is not None
        ws.close()
