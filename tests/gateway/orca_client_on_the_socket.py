"""orca-client on the far end of a daemon's /ws/devices socket, built from cheshire-drivers as orca-client builds it."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from cheshire_drivers.command_executor import CommandExecutor, DeviceDrivers, DeviceRegistry, answer_command
from cheshire_drivers.driver_lookup import DriverRequest, build_real_driver, build_sim_driver
from cheshire_drivers.gateway_protocol import (
    AssignedDevice,
    AssignmentResult,
    ClientHello,
    CommandMessage,
    DeviceAssignment,
    MessageEnvelope,
    ResponseMessage,
    StatusMessage,
)
from fastapi import FastAPI
from starlette.testclient import WebSocketTestSession


async def orca_client_drivers(device: AssignedDevice) -> DeviceDrivers:
    """What orca-client's DeviceFactory builds: no live driver for an always_sim device."""
    request = DriverRequest.for_device(device)
    live = None if device.setup.real is None or device.setup.always_sim else build_real_driver(request)
    return DeviceDrivers(live=live, sim=build_sim_driver(request))


class OrcaClientOnTheSocket:
    """orca-client on the far end of the daemon's socket: cheshire-drivers' registry and executor."""

    def __init__(
        self, ws: WebSocketTestSession, client_id: str,
        build: Callable[[AssignedDevice], Awaitable[DeviceDrivers]] = orca_client_drivers,
    ) -> None:
        self._ws = ws
        self._client_id = client_id
        self._build = build
        self._loop = asyncio.new_event_loop()
        self._registry = DeviceRegistry()
        self._executor = CommandExecutor(self._registry)

    def connect(self) -> None:
        self._ws.send_text(MessageEnvelope.wrap_hello(ClientHello(client_id=self._client_id, site="test", lab="simulation")).model_dump_json())
        assignment = MessageEnvelope.model_validate_json(self._ws.receive_text()).unwrap()
        assert isinstance(assignment, DeviceAssignment)
        errors = self._loop.run_until_complete(self._registry.apply(assignment.devices, self._build))
        result = AssignmentResult(
            assignment_id=assignment.assignment_id, devices=self._registry.connect_info_list(), errors=errors,
        )
        self._ws.send_text(MessageEnvelope.wrap_assignment_result(result).model_dump_json())
        self._loop.run_until_complete(self._report_status())

    async def _report_status(self) -> None:
        status = StatusMessage(devices=self._registry.status_report(), timestamp=time.time())
        self._ws.send_text(MessageEnvelope.wrap_status(status).model_dump_json())

    async def _respond(self, response: ResponseMessage) -> None:
        self._ws.send_text(MessageEnvelope.wrap_response(response).model_dump_json())

    def answer_one_command(self) -> None:
        while True:
            message = MessageEnvelope.model_validate_json(self._ws.receive_text()).unwrap()
            if isinstance(message, CommandMessage):
                break
        self._loop.run_until_complete(
            answer_command(self._executor, message, respond=self._respond, report_status=self._report_status),
        )


def await_connected(app: FastAPI, name: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = app.state.daemon.connections.gateway.tracker.peek_snapshot(name)
        if snapshot is not None and snapshot.link_mode is not None:
            return
        time.sleep(0.01)
    raise AssertionError(f"{name!r} did not connect within {timeout}s")
