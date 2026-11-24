"""A sim run is refused at its start when one device advertises fewer interfaces than its setup says, naming that device.

Thread bodies do not declare their devices, so a run checks every device. `simple_workflow` never
uses `lh`, and it is still refused: an interface mismatch is a setup error, and it stays a loud refusal.
"""

import pytest
from cheshire_drivers.command_executor import DeviceDrivers
from cheshire_drivers.gateway_protocol import AssignedDevice
from cheshire_drivers.sims import SimPLRLiquidHandlerDriver
from fastapi.testclient import TestClient

from orca.daemon.app import create_app
from tests.daemon.liquid_handler_package import write_liquid_handler_package
from tests.gateway.orca_client_on_the_socket import OrcaClientOnTheSocket, await_connected

_CLIENT = "lab_pc"


async def _a_plain_liquid_handler(device: AssignedDevice) -> DeviceDrivers:
    return DeviceDrivers(live=None, sim=SimPLRLiquidHandlerDriver(device.name))


@pytest.mark.out_of_run
def test_a_sim_run_is_refused_naming_the_device_whose_interfaces_do_not_match() -> None:
    write_liquid_handler_package({"client": _CLIENT, "real": {"type": "plr", "backend": "STAR"}})
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        mounted = client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        try:
            with client.websocket_connect("/ws/devices") as ws:
                OrcaClientOnTheSocket(ws, _CLIENT, _a_plain_liquid_handler).connect()
                await_connected(app, "lh")

                run = client.post("/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "SIM"})
                ws.close()
        finally:
            client.post("/api/runtime/unload", json={})

    assert run.status_code == 409, run.text
    detail = run.json()["detail"]
    assert detail["code"] == "device_interface_mismatch"
    assert detail["extras"] == {"device_id": "lh"}
    assert detail["message"].startswith("Device 'lh':")
