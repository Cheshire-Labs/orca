"""An operator command reaches a device set always_sim whose setup names a real STAR or OT2.

orca-client builds no live driver for an always_sim device, so it runs and advertises only the simulator.
The command must reach that simulator.
"""

import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from orca.daemon.app import create_app
from tests.daemon.liquid_handler_package import write_liquid_handler_package
from tests.gateway.orca_client_on_the_socket import OrcaClientOnTheSocket, await_connected

_CLIENT = "lab_pc"
_SETUPS: dict[str, dict[str, JsonValue]] = {
    "no real driver": {"client": _CLIENT},
    "always_sim STAR": {"client": _CLIENT, "real": {"type": "plr", "backend": "STAR"}, "always_sim": True},
    "always_sim OT2": {
        "client": _CLIENT, "always_sim": True,
        "real": {"type": "plr", "backend": "OpentronsOT2Backend", "connection": {"type": "tcp", "host": "10.0.0.5"}},
    },
}


@pytest.mark.out_of_run
@pytest.mark.parametrize("setup", sorted(_SETUPS))
def test_an_operator_command_reaches_an_always_sim_device_with_a_real_driver(setup: str) -> None:
    write_liquid_handler_package(_SETUPS[setup])
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        mounted = client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        try:
            with client.websocket_connect("/ws/devices") as ws:
                orca_client = OrcaClientOnTheSocket(ws, _CLIENT)
                orca_client.connect()
                await_connected(app, "lh")
                answering = threading.Thread(target=orca_client.answer_one_command, daemon=True)
                answering.start()

                sent = client.post("/api/devices/lh/commands", json={"commands": [{"command": "get_deck_state", "params": {}}]})

                answering.join(timeout=10.0)
                ws.close()
        finally:
            client.post("/api/runtime/unload", json={})

    assert sent.status_code == 200, sent.text
    assert sent.json()["results"][0]["success"] is True, sent.text
