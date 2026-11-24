"""A server can wait for a client to report its build, and its authenticator decides which client ID a key may use."""

import asyncio
import threading

import pytest
from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.gateway_protocol import (
    AssignedDevice,
    AssignmentResult,
    ClientHello,
    DeviceAssignment,
    MessageEnvelope,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orca.gateway.websocket.assignments import DeviceAssignments
from orca.gateway.websocket.manager import ConnectionManager
from orca.gateway.websocket.router import create_gateway_router
from tests.gateway_parts import gateway_parts


class _OnlyItsOwnId:
    """A key may use only the client ID it was issued for."""

    async def authenticate(self, api_key: str | None) -> bool:
        return True

    async def may_claim(self, api_key: str | None, client_id: str) -> bool:
        return api_key == f"key-for-{client_id}"


def _device(client: str) -> AssignedDevice:
    return AssignedDevice(name="d", kind="shaker", setup=DeviceSetup.model_validate({"client": client}))


def _latest_id(table: DeviceAssignments) -> str:
    assignment = table.for_client("lab_pc")
    assert assignment is not None
    return assignment.assignment_id


def _hello(client_id: str) -> str:
    return MessageEnvelope.wrap_hello(ClientHello(client_id=client_id, site="s", lab="l")).model_dump_json()


def test_a_key_the_authenticator_does_not_allow_that_client_id_is_closed() -> None:
    app = FastAPI()
    app.include_router(create_gateway_router(gateway_parts().gateway, authenticator=_OnlyItsOwnId()))

    with TestClient(app) as client, client.websocket_connect("/ws/devices", headers={"X-API-Key": "key-for-lab_pc"}) as ws:
        ws.send_text(_hello("builtin"))
        with pytest.raises(WebSocketDisconnect) as refused:
            ws.receive_text()

    assert refused.value.code == 1008


def test_a_clients_report_over_the_socket_ends_the_wait_for_its_build() -> None:
    parts = gateway_parts()
    asyncio.run(parts.assignments.publish([_device("lab_pc")]))
    app = FastAPI()
    app.include_router(create_gateway_router(parts.gateway, authenticator=_OnlyItsOwnId()))
    waited: list[bool] = []

    with TestClient(app) as client, client.websocket_connect("/ws/devices", headers={"X-API-Key": "key-for-lab_pc"}) as ws:
        assert client.portal is not None
        portal = client.portal
        waiter = threading.Thread(target=lambda: waited.append(portal.call(parts.assignments.wait_for_build, "lab_pc", 10.0)))
        waiter.start()
        ws.send_text(_hello("lab_pc"))
        assignment = MessageEnvelope.model_validate_json(ws.receive_text()).unwrap()
        assert isinstance(assignment, DeviceAssignment)
        result = AssignmentResult(assignment_id=assignment.assignment_id, devices=[])
        ws.send_text(MessageEnvelope.wrap_assignment_result(result).model_dump_json())
        waiter.join(timeout=15)

    assert waited == [True]


async def test_waiting_for_a_build_returns_once_that_client_has_reported() -> None:
    table = DeviceAssignments(ConnectionManager())
    await table.publish([_device("lab_pc")])
    waiting = asyncio.create_task(table.wait_for_build("lab_pc", timeout=5.0))
    await asyncio.sleep(0)

    table.record_build("lab_pc", _latest_id(table), applied=True)

    assert await waiting


async def test_another_clients_report_does_not_end_the_wait() -> None:
    table = DeviceAssignments(ConnectionManager())
    await table.publish([_device("lab_pc")])

    table.record_build("other_pc", _latest_id(table), applied=True)

    assert not await table.wait_for_build("lab_pc", timeout=0.01)


async def test_a_report_on_an_earlier_assignment_does_not_end_the_wait_on_a_new_one() -> None:
    table = DeviceAssignments(ConnectionManager())
    await table.publish([_device("lab_pc")])
    table.record_build("lab_pc", _latest_id(table), applied=True)

    await table.publish([_device("lab_pc")])

    assert not await table.wait_for_build("lab_pc", timeout=0.01)


async def test_a_report_that_names_an_older_assignment_does_not_end_the_wait_on_the_latest() -> None:
    table = DeviceAssignments(ConnectionManager())
    await table.publish([_device("lab_pc")])
    older = _latest_id(table)
    await table.publish([_device("lab_pc")])

    table.record_build("lab_pc", older, applied=True)
    assert not await table.wait_for_build("lab_pc", timeout=0.01)

    table.record_build("lab_pc", _latest_id(table), applied=True)
    assert await table.wait_for_build("lab_pc", timeout=0.01)


async def test_each_build_gets_a_new_assignment_id() -> None:
    table = DeviceAssignments(ConnectionManager())
    await table.publish([_device("lab_pc")])
    first = _latest_id(table)

    await table.publish([_device("lab_pc")])

    assert _latest_id(table) != first
    assert table.is_latest(_latest_id(table)) and not table.is_latest(first)


async def test_a_client_the_latest_assignment_gives_nothing_is_not_waited_for() -> None:
    table = DeviceAssignments(ConnectionManager())
    await table.publish([_device("other_pc")])

    assert await asyncio.wait_for(table.wait_for_build("lab_pc", timeout=60.0), timeout=1.0)
