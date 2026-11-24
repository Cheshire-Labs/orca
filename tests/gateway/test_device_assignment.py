"""The package says which orca-client runs each device, and the websocket hands each client its part."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import ParamSpec, TypeVar

import pytest
from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.driver_lookup import driver_interfaces, sim_driver_interfaces
from cheshire_drivers.gateway_protocol import (
    CLIENT_ID_IN_USE_CLOSE_CODE,
    AssignedDevice,
    AssignmentResult,
    ClientHello,
    DeviceAssignment,
    DeviceConnectInfo,
    MessageEnvelope,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from orca.gateway.controller.controller import _why_offline
from orca.gateway.auth import NullConnectionAuthenticator
from orca.gateway.websocket.router import create_gateway_router
from orca.resource_models.plate_hotel import PlateHotel
from orca.resource_models.transporter import Transporter
from orca.runtime.device_assignment import assign_devices, interfaces_from_setups, simulator_interfaces_from_setups
from orca.runtime.setup_file_stores import DeviceSetupFileStore
from orca.runtime.setup_files import SetupFileError
from orca.sdk.devices import Sealer, Shaker
from tests.gateway_parts import GatewayParts, gateway_parts
from cheshire_drivers.device_setup import BUILTIN_CLIENT


@pytest.fixture
def parts() -> GatewayParts:
    return gateway_parts()


def _store(tmp_path: Path, **files: str) -> DeviceSetupFileStore:
    directory = tmp_path / "devices"
    directory.mkdir()
    for name, text in files.items():
        (directory / f"{name}.json").write_text(text, encoding="utf-8")
    return DeviceSetupFileStore(directory, BUILTIN_CLIENT)


_LAB_PC_SEALER = (
    '{"client": "lab_pc", "real": {"type": "plr", "backend": "A4SBackend", '
    '"connection": {"type": "serial", "port": "COM3"}}}'
)


def test_a_device_with_no_setup_file_goes_to_the_built_in_client_with_a_default_sim(tmp_path: Path) -> None:
    assigned = assign_devices([Shaker("shaker_1")], _store(tmp_path))

    assert assigned == [AssignedDevice(name="shaker_1", kind="shaker", setup=DeviceSetup(client=BUILTIN_CLIENT))]


def test_a_setup_whose_client_is_null_goes_to_the_default_client(tmp_path: Path) -> None:
    assigned = assign_devices([Shaker("shaker_1")], _store(tmp_path, shaker_1='{"client": null}'))

    assert assigned[0].setup.client == BUILTIN_CLIENT


def test_a_real_driver_naming_no_client_is_refused_when_the_default_is_the_built_in_client(tmp_path: Path) -> None:
    no_client = '{"real": {"type": "plr", "backend": "A4SBackend", "connection": {"type": "serial", "port": "COM3"}}}'

    with pytest.raises(ValueError, match=r"(?s)sealer_1\.json names no client.*'builtin'.*lab PC"):
        assign_devices([Sealer("sealer_1")], _store(tmp_path, sealer_1=no_client))


def test_a_transporter_is_assigned_like_any_device(tmp_path: Path) -> None:
    assigned = assign_devices([Transporter("pf400_1")], _store(tmp_path, pf400_1='{"client": "lab_pc"}'))

    assert [(device.name, device.kind, device.setup.client) for device in assigned] == [("pf400_1", "transporter", "lab_pc")]


def test_a_setup_file_names_the_client_that_runs_the_device(tmp_path: Path) -> None:
    assigned = assign_devices([Sealer("sealer_1")], _store(tmp_path, sealer_1=_LAB_PC_SEALER))

    assert assigned[0].setup.client == "lab_pc"


def test_a_setup_file_naming_no_device_in_the_topology_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ghost.json names no device"):
        assign_devices([Shaker("shaker_1")], _store(tmp_path, ghost="{}"))


def test_a_setup_that_does_not_match_its_kind_is_refused_when_the_package_loads(tmp_path: Path) -> None:
    human = '{"client": "lab_pc", "real": {"type": "human"}}'

    with pytest.raises(ValueError, match="shaker_1"):
        assign_devices([Shaker("shaker_1")], _store(tmp_path, shaker_1=human))


def test_an_invalid_setup_file_is_refused_naming_the_file_and_the_field(tmp_path: Path) -> None:
    with pytest.raises(SetupFileError, match=r"(?s)shaker_1\.json.*axis_speed"):
        assign_devices([Shaker("shaker_1")], _store(tmp_path, shaker_1='{"axis_speed": 100}'))


def test_reading_a_devices_interfaces_names_the_device_whose_setup_cannot_work(tmp_path: Path) -> None:
    store = _store(tmp_path, shaker_1='{"client": "lab_pc", "real": {"type": "plr", "backend": "NoSuchBackend"}}')

    with pytest.raises(ValueError, match="shaker_1"):
        interfaces_from_setups(store)("shaker", "shaker_1", False)


_REAL_STAR = '{"client": "lab_pc", "real": {"type": "plr", "backend": "STAR"}}'
_ALWAYS_SIM_STAR = '{"client": "lab_pc", "real": {"type": "plr", "backend": "STAR"}, "always_sim": true}'


def test_the_server_expects_a_real_stars_interfaces_whatever_its_setting(tmp_path: Path) -> None:
    store = _store(tmp_path, star=_REAL_STAR)
    expected = interfaces_from_setups(store)("liquid_handler", "star", False)
    assert "IGripperMotion" in expected
    assert expected == driver_interfaces("liquid_handler", store.read("star"))


def test_the_server_expects_an_always_sim_stars_simulator_interfaces(tmp_path: Path) -> None:
    store = _store(tmp_path, star=_ALWAYS_SIM_STAR)
    expected = interfaces_from_setups(store)("liquid_handler", "star", False)
    assert "IGripperMotion" in expected
    assert expected == sim_driver_interfaces("liquid_handler", store.read("star"))


def test_a_check_run_expects_a_real_stars_simulator_interfaces(tmp_path: Path) -> None:
    store = _store(tmp_path, star=_REAL_STAR)
    expected = simulator_interfaces_from_setups(store)("liquid_handler", "star", True)
    assert "IGripperMotion" in expected
    assert expected == sim_driver_interfaces("liquid_handler", store.read("star"), True)


def test_a_rack_with_no_setup_file_is_assigned_no_driver(tmp_path: Path) -> None:
    assert assign_devices([PlateHotel("hotel_1", shelves=2)], _store(tmp_path)) == []


def test_a_rack_takes_no_setup_file(tmp_path: Path) -> None:
    """A rack has no driver, so a file for it is a mistake; the message says which file to remove."""
    with pytest.raises(ValueError, match=r"hotel_1.*no setup file.*devices/hotel_1\.json"):
        assign_devices([PlateHotel("hotel_1", shelves=2)], _store(tmp_path, hotel_1='{"client": "lab_pc"}'))


P = ParamSpec("P")
R = TypeVar("R")


def _call(client: TestClient, fn: Callable[P, Awaitable[R]], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run `fn` on the app's event loop, where the websocket handlers run."""
    assert client.portal is not None
    return client.portal.call(partial(fn, *args, **kwargs))


def _app(parts: GatewayParts) -> FastAPI:
    app = FastAPI()
    app.include_router(create_gateway_router(parts.gateway, authenticator=NullConnectionAuthenticator()))
    return app


def _hello(client_id: str = "lab_pc") -> str:
    return MessageEnvelope.wrap_hello(ClientHello(client_id=client_id, site="boston", lab="molbio")).model_dump_json()


def _shaker(client: str = "lab_pc", name: str = "shaker_1") -> AssignedDevice:
    return AssignedDevice(name=name, kind="shaker", setup=DeviceSetup.model_validate({"client": client}))


def _assignment_from(text: str) -> DeviceAssignment:
    message = MessageEnvelope.model_validate_json(text).unwrap()
    assert isinstance(message, DeviceAssignment)
    return message


def _publish(parts: GatewayParts, devices: list[AssignedDevice]) -> None:
    asyncio.run(parts.assignments.publish(devices))


def _latest(parts: GatewayParts) -> str:
    assignment = parts.assignments.for_client("lab_pc")
    assert assignment is not None
    return assignment.assignment_id


def _result(assignment_id: str, *names: str, errors: dict[str, str] | None = None, error: str | None = None) -> str:
    built = [DeviceConnectInfo(name=name, type="shaker", interfaces=frozenset({"IShaker"})) for name in names]
    result = AssignmentResult(assignment_id=assignment_id, devices=built, errors=errors or {}, error=error)
    return MessageEnvelope.wrap_assignment_result(result).model_dump_json()


def test_before_any_build_a_client_has_no_assignment(parts: GatewayParts) -> None:
    assert parts.assignments.for_client("lab_pc") is None


def test_a_client_is_sent_only_its_own_devices(parts: GatewayParts) -> None:
    _publish(parts, [_shaker("lab_pc", "mine"), _shaker("other_pc", "theirs")])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello())
        assignment = _assignment_from(ws.receive_text())

    assert [device.name for device in assignment.devices] == ["mine"]


def test_an_unknown_client_is_sent_nothing_to_run_and_stays_connected(parts: GatewayParts) -> None:
    _publish(parts, [_shaker("lab_pc")])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello("stranger"))

        assert _assignment_from(ws.receive_text()).devices == []
        assert "stranger" in parts.connections.connected_client_ids()


def test_what_the_client_built_comes_online(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello())
        ws.receive_text()
        ws.send_text(_result(_latest(parts), "shaker_1"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

        assert _call(client, parts.tracker.is_device_online, "shaker_1")


def test_a_device_that_failed_to_build_stays_offline_with_its_error(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello())
        ws.receive_text()
        ws.send_text(_result(_latest(parts), errors={"shaker_1": "no such backend"}))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

        assert not _call(client, parts.tracker.is_device_online, "shaker_1")
        assert parts.tracker.build_error("shaker_1") == "no such backend"


def test_a_new_build_is_sent_to_a_connected_client_and_a_device_it_drops_goes_offline(parts: GatewayParts) -> None:
    _publish(parts, [_shaker(name="old")])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello())
        ws.receive_text()
        ws.send_text(_result(_latest(parts), "old"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

        _call(client, parts.assignments.publish, [_shaker(name="new")])
        assert [device.name for device in _assignment_from(ws.receive_text()).devices] == ["new"]

        ws.send_text(_result(_latest(parts), "new"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()
        assert _call(client, parts.tracker.is_device_online, "new")
        assert not _call(client, parts.tracker.is_device_online, "old")


def test_a_result_for_an_older_assignment_is_ignored_so_it_cannot_mark_the_latest_as_built(parts: GatewayParts) -> None:
    _publish(parts, [_shaker(name="old")])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello())
        older = _assignment_from(ws.receive_text()).assignment_id
        _call(client, parts.assignments.publish, [_shaker(name="new")])
        ws.receive_text()

        ws.send_text(_result(older, "old"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

        assert not _call(client, parts.tracker.is_device_online, "old")
        assert not _call(client, parts.assignments.wait_for_build, "lab_pc", 0.01)

        ws.send_text(_result(_latest(parts), "new"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

        assert _call(client, parts.tracker.is_device_online, "new")
        assert _call(client, parts.assignments.wait_for_build, "lab_pc", 0.01)


def test_an_assignment_the_client_could_not_apply_ends_the_wait_and_says_why_on_each_device(parts: GatewayParts) -> None:
    _publish(parts, [_shaker(name="shaker_1"), _shaker(name="shaker_2")])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello())
        ws.receive_text()
        ws.send_text(_result(_latest(parts), error="RuntimeError: factory broke"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

        assert _call(client, parts.assignments.wait_for_build, "lab_pc", 0.01)
        for name in ("shaker_1", "shaker_2"):
            assert not _call(client, parts.tracker.is_device_online, name)
            assert "factory broke" in (parts.tracker.build_error(name) or "")


def test_a_second_connection_with_a_client_id_still_heartbeating_is_refused_and_can_retry(parts: GatewayParts) -> None:
    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as first:
        first.send_text(_hello())
        _call(client, parts.connections.get_connection, "lab_pc")

        with client.websocket_connect("/ws/devices") as second:
            second.send_text(_hello())
            with pytest.raises(WebSocketDisconnect) as refused:
                second.receive_text()

        assert refused.value.code == CLIENT_ID_IN_USE_CLOSE_CODE
        assert "lab_pc" in parts.connections.connected_client_ids()


def test_a_connection_that_stopped_heartbeating_is_replaced(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as first:
        first.send_text(_hello())
        first.receive_text()
        connection = _call(client, parts.connections.get_connection, "lab_pc")
        assert connection is not None
        connection.last_heartbeat = datetime.now(timezone.utc) - timedelta(hours=1)

        with client.websocket_connect("/ws/devices") as second:
            second.send_text(_hello())

            assert _assignment_from(second.receive_text()).devices == [_shaker()]


def test_a_replaced_sockets_cleanup_leaves_the_new_connections_devices_online(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as first:
        first.send_text(_hello())
        first.receive_text()
        connection = _call(client, parts.connections.get_connection, "lab_pc")
        assert connection is not None
        connection.last_heartbeat = datetime.now(timezone.utc) - timedelta(hours=1)

        with client.websocket_connect("/ws/devices") as second:
            second.send_text(_hello())
            second.receive_text()
            second.send_text(_result(_latest(parts), "shaker_1"))
            _call(client, parts.assignments.send_to, "lab_pc")
            second.receive_text()

            first.close()
            for _ in range(20):
                _call(client, asyncio.sleep, 0.01)

            assert _call(client, parts.tracker.is_device_online, "shaker_1")


def test_a_command_to_a_device_that_failed_to_build_says_why(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        ws.send_text(_hello())
        ws.receive_text()
        ws.send_text(_result(_latest(parts), errors={"shaker_1": "no such backend"}))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

        assert "no such backend" in _why_offline(parts.tracker, "shaker_1")


def test_a_web_page_cannot_connect_to_the_device_route(parts: GatewayParts) -> None:
    with TestClient(_app(parts)) as client:
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/ws/devices", headers={"Origin": "https://evil.example"}) as ws:
                ws.receive_text()

    assert refused.value.code == 1008


class _Seen:
    def __init__(self, parts: GatewayParts) -> None:
        self.connected: list[str] = []
        self.disconnected: list[str] = []
        parts.events.subscribe_connected(self._on_connected)
        parts.events.subscribe_disconnected(self._on_disconnected)

    async def _on_connected(self, device: DeviceConnectInfo, _client_id: str) -> None:
        self.connected.append(device.name)

    async def _on_disconnected(self, device_id: str) -> None:
        self.disconnected.append(device_id)


def _connect_one_shaker(client: TestClient, ws: WebSocketTestSession, parts: GatewayParts) -> None:
    ws.send_text(_hello())
    ws.receive_text()
    ws.send_text(_result(_latest(parts), "shaker_1"))
    _call(client, parts.assignments.send_to, "lab_pc")
    ws.receive_text()


def test_a_device_still_attached_is_not_announced_connected_again_on_a_new_build(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])
    seen = _Seen(parts)

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        _connect_one_shaker(client, ws, parts)
        _call(client, parts.assignments.publish, [_shaker()])
        ws.receive_text()
        ws.send_text(_result(_latest(parts), "shaker_1"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

    assert seen.connected == ["shaker_1"]


def test_a_device_rebuilt_from_a_changed_setup_is_announced_connected_again(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])
    seen = _Seen(parts)
    changed = AssignedDevice(
        name="shaker_1", kind="shaker", setup=DeviceSetup.model_validate({"client": "lab_pc", "always_sim": True}),
    )

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        _connect_one_shaker(client, ws, parts)
        _call(client, parts.assignments.publish, [changed])
        ws.receive_text()
        ws.send_text(_result(_latest(parts), "shaker_1"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

    assert seen.connected == ["shaker_1", "shaker_1"]


def test_a_changed_setup_the_client_could_not_apply_is_announced_when_it_is_built_later(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])
    seen = _Seen(parts)
    changed = AssignedDevice(
        name="shaker_1", kind="shaker", setup=DeviceSetup.model_validate({"client": "lab_pc", "always_sim": True}),
    )

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as ws:
        _connect_one_shaker(client, ws, parts)
        _call(client, parts.assignments.publish, [changed])
        ws.receive_text()
        ws.send_text(_result(_latest(parts), "shaker_1", error="malformed assignment"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()
        assert seen.connected == ["shaker_1"]
        _call(client, parts.assignments.publish, [changed])
        ws.receive_text()
        ws.send_text(_result(_latest(parts), "shaker_1"))
        _call(client, parts.assignments.send_to, "lab_pc")
        ws.receive_text()

    assert seen.connected == ["shaker_1", "shaker_1"]


def _wait_until_unregistered(client: TestClient, parts: GatewayParts) -> None:
    for _ in range(200):
        if "lab_pc" not in parts.connections.connected_client_ids():
            return
        _call(client, asyncio.sleep, 0.01)
    raise AssertionError("the router never cleaned up the dropped connection")


def test_a_client_whose_send_failed_is_unregistered_and_its_devices_reported_disconnected(
    parts: GatewayParts, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(parts, [_shaker()])
    seen = _Seen(parts)

    with TestClient(_app(parts)) as client:
        with client.websocket_connect("/ws/devices") as ws:
            _connect_one_shaker(client, ws, parts)
            connection = _call(client, parts.connections.get_connection, "lab_pc")
            assert connection is not None

            async def broken_send(_text: str) -> None:
                raise ConnectionResetError("socket gone")

            monkeypatch.setattr(connection.websocket, "send_text", broken_send)
            assert not _call(client, parts.connections.send_to_client, "lab_pc", "{}")
        _wait_until_unregistered(client, parts)

        assert not _call(client, parts.tracker.is_device_online, "shaker_1")
    assert seen.disconnected == ["shaker_1"]


def test_a_stale_client_is_unregistered_and_its_devices_reported_disconnected_once(parts: GatewayParts) -> None:
    _publish(parts, [_shaker()])
    seen = _Seen(parts)

    with TestClient(_app(parts)) as client:
        with client.websocket_connect("/ws/devices") as ws:
            _connect_one_shaker(client, ws, parts)
            connection = _call(client, parts.connections.get_connection, "lab_pc")
            assert connection is not None
            connection.last_heartbeat = datetime.now(timezone.utc) - timedelta(hours=1)

            assert _call(client, parts.connections.close_stale_connections, 90) == ["lab_pc"]
        _wait_until_unregistered(client, parts)

        assert not _call(client, parts.tracker.is_device_online, "shaker_1")
    assert seen.disconnected == ["shaker_1"]


def test_a_client_whose_send_failed_can_reconnect_before_its_old_socket_is_cleaned_up(
    parts: GatewayParts, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(parts, [_shaker()])

    with TestClient(_app(parts)) as client, client.websocket_connect("/ws/devices") as first:
        _connect_one_shaker(client, first, parts)
        connection = _call(client, parts.connections.get_connection, "lab_pc")
        assert connection is not None

        async def broken_send(_text: str) -> None:
            raise ConnectionResetError("socket gone")

        monkeypatch.setattr(connection.websocket, "send_text", broken_send)
        assert not _call(client, parts.connections.send_to_client, "lab_pc", "{}")

        with client.websocket_connect("/ws/devices") as second:
            second.send_text(_hello())

            assert _assignment_from(second.receive_text()).devices == [_shaker()]
