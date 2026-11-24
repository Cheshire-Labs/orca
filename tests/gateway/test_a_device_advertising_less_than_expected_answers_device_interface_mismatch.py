"""A device whose orca-client advertises fewer interfaces than its setup says answers `device_interface_mismatch`, never a 500 or a bare conflict.

The setup names a real STAR that is not always_sim, so the server expects the real STAR's interfaces.
The orca-client here holds a plain liquid handler, which lacks the STAR's gripper interfaces.
"""

import pytest
from cheshire_drivers.command_executor import DeviceDrivers
from cheshire_drivers.gateway_protocol import AssignedDevice
from cheshire_drivers.sims import SimPLRLiquidHandlerDriver
from fastapi.testclient import TestClient
from httpx import Response

from orca.daemon.app import create_app
from orca.operations.submission import submit_refusal
from orca.runtime.errors import TopologyCollisionError
from tests.daemon.liquid_handler_package import write_liquid_handler_package
from tests.gateway.orca_client_on_the_socket import OrcaClientOnTheSocket, await_connected

_CLIENT = "lab_pc"


async def _a_plain_liquid_handler(device: AssignedDevice) -> DeviceDrivers:
    return DeviceDrivers(live=None, sim=SimPLRLiquidHandlerDriver(device.name))


def _names_the_mismatch(answer: Response) -> None:
    assert answer.status_code == 409, answer.text
    detail = answer.json()["detail"]
    assert detail["code"] == "device_interface_mismatch"
    assert detail["extras"] == {"device_id": "lh"}
    assert "IGripperMotion" in detail["message"]


@pytest.mark.out_of_run
def test_every_operator_route_on_the_device_is_refused_naming_it() -> None:
    write_liquid_handler_package({"client": _CLIENT, "real": {"type": "plr", "backend": "STAR"}})
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        mounted = client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        try:
            with client.websocket_connect("/ws/devices") as ws:
                OrcaClientOnTheSocket(ws, _CLIENT, _a_plain_liquid_handler).connect()
                await_connected(app, "lh")

                command = client.post("/api/devices/lh/commands", json={"commands": [{"command": "home", "params": {}}]})
                initialized = client.post("/api/devices/lh/initialize", json={})
                shown = client.get("/api/devices/lh")
                ws.close()
        finally:
            client.post("/api/runtime/unload", json={})

    _names_the_mismatch(command)
    _names_the_mismatch(initialized)
    _names_the_mismatch(shown)


def test_a_submission_refused_by_it_names_the_device() -> None:
    refusal = submit_refusal(TopologyCollisionError("lh", "Device 'lh': missing ['IGripperMotion']"))
    assert (refusal.surface_code(), refusal.http_status, refusal.extras) == (
        "device_interface_mismatch", 409, {"device_id": "lh"},
    )
