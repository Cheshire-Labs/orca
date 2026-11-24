"""Tests for device-write routes + introspection.

Covers:
- GET /devices/{id}/introspection: what the device can run, with each command's danger level.
- POST /devices/{id}/commands: sends commands by name through the operator path.
- POST /devices/{id}/initialize: brings up + resets. Asks for no motion.
- POST /devices/{id}/connect, /disconnect: the two ends of holding
  a device, neither of which moves it.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from httpx import AsyncClient
from pydantic import JsonValue

from orca.cli.device import app as device_cli
from orca.gateway.controller.exceptions import (
    CommandTimeoutError,
    DeviceOfflineError,
)


def _one(command: str, params: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    return {"commands": [{"command": command, "params": params or {}}]}


# -- Gating ------------------------------------------------------------------


async def test_device_introspection_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/api/devices/shaker1/introspection")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"


async def test_device_execute_with_no_runtime_names_an_unknown_device(empty_client: AsyncClient) -> None:
    """With nothing built, a device no orca-client has connected is unknown; a known one is refused as not ready."""
    resp = await empty_client.post("/api/devices/shaker1/commands", json=_one("noop"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "device_not_found"


async def test_device_initialize_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post("/api/devices/shaker1/initialize", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"


# -- Unknown device ---------------------------------------------------------


async def test_device_introspection_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/devices/no_such_device/introspection")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "device_not_found"


async def test_device_execute_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/api/devices/no_such_device/commands", json=_one("noop"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "device_not_found"
    assert resp.json()["detail"]["extras"]["device_id"] == "no_such_device"


@pytest.mark.parametrize("command", ["shake", "shaker.shake"])
async def test_device_execute_sends_the_method_through_the_operator_path(
    client: AsyncClient, command: str,
) -> None:
    """The bare name and the orca-side namespaced form both reach the device as `shake`, through the runner."""
    with _connected("shaker1"), _sent_through_the_runner() as sent:
        resp = await client.post(
            "/api/devices/shaker1/commands",
            json=_one(command, {"speed": 500, "duration": 1}),
        )
    assert resp.status_code == 200, resp.text
    assert sent.await_args_list[-1].args[1:3] == ("shake", {"speed": 500, "duration": 1})


async def test_device_initialize_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post("/api/devices/no_such_device/initialize", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "device_not_found"
    assert resp.json()["detail"]["extras"]["device_id"] == "no_such_device"


# -- Introspection ----------------------------------------------------------


async def test_device_introspection_carries_each_commands_danger_level(client: AsyncClient) -> None:
    """The fixture's shaker1 implements UniversalMockDevice, which satisfies several capability
    interfaces; each command it can be sent by name carries the danger level an operator reads before sending."""
    resp = await client.get("/api/devices/shaker1/introspection")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "shaker1"
    # Keyed as `methods` is, so a method's danger is found under its own name.
    assert body["danger_levels"]["shake"] == "PHYSICAL"
    assert "shake" in body["methods"]


# -- Unified device registry -------------------------------------------------


async def test_device_registry_list_returns_wrapped_payload(
    client: AsyncClient,
) -> None:
    """`GET /devices` returns `{"devices": [...]}` shape mirroring
    a hosted deployment's REST surface. Each entry carries the two-card view + live
    state + per-mode eligibility."""
    resp = await client.get("/api/devices")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, dict)
    assert "devices" in payload
    assert isinstance(payload["devices"], list)
    assert payload["devices"], "fixture exposes at least one device"
    entry = payload["devices"][0]
    assert "name" in entry
    # Two connections, reported separately: the client wire and the device link.
    assert "is_client_connected" in entry
    assert "is_device_connected" in entry
    assert "is_initialized" in entry
    # The two flags describe one driver; without this an operator cannot tell
    # a simulator's open link from the instrument's.
    assert "device_link_mode" in entry
    assert "mode_eligibility" in entry
    eligibility = entry["mode_eligibility"]
    assert {"check_run", "sim", "live"} <= set(eligibility.keys())


async def test_device_registry_show_known_device_returns_entry(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/devices/shaker1")
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["name"] == "shaker1"
    assert "topology_card" in entry
    assert "mode_eligibility" in entry


async def test_device_registry_show_unknown_device_404s(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/devices/no_such_device")
    assert resp.status_code == 404


# -- Initialize --------------------------------------------------------------


def _sent_through_the_runner() -> AbstractContextManager[AsyncMock]:
    """The runner's send stage, stood in for below its audit row: (device, command, params) per call."""
    return patch(
        "orca.gateway.operator_commands.OperatorDeviceCommands._send",
        new=AsyncMock(return_value=(None, 0.0)),
    )


async def test_device_initialize_succeeds(client: AsyncClient) -> None:
    with _sent_through_the_runner() as sent:
        resp = await client.post("/api/devices/shaker1/initialize", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "initialized"
    assert sent.await_args is not None
    assert sent.await_args.args[:2] == ("shaker1", "initialize")


# -- Connect / disconnect ----------------------------------------------------


async def test_device_connect_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post("/api/devices/shaker1/connect", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"


async def test_device_disconnect_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post("/api/devices/shaker1/disconnect", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"


async def test_device_connect_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/api/devices/no_such_device/connect", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "device_not_found"
    assert resp.json()["detail"]["extras"]["device_id"] == "no_such_device"


async def test_device_disconnect_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/api/devices/no_such_device/disconnect", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "device_not_found"
    assert resp.json()["detail"]["extras"]["device_id"] == "no_such_device"


async def test_device_connect_succeeds(client: AsyncClient) -> None:
    with _sent_through_the_runner() as sent:
        resp = await client.post("/api/devices/shaker1/connect", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "connected"
    assert sent.await_args is not None
    assert sent.await_args.args[:2] == ("shaker1", "connect")


async def test_device_disconnect_succeeds(client: AsyncClient) -> None:
    with _sent_through_the_runner() as sent:
        resp = await client.post("/api/devices/shaker1/disconnect", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disconnected"
    assert sent.await_args is not None
    assert sent.await_args.args[:2] == ("shaker1", "disconnect")


def _connected_card(name: str = "flex_remote"):
    """A device bridge advertising one vendor command. For flex_remote, a device topology never declared."""
    from datetime import datetime

    from orca.gateway.registry.snapshot import DeviceSnapshot

    return DeviceSnapshot(
        type="FlexLiquidHandlerDriver", name=name,
        interfaces=["ILiquidHandler"], capabilities=["gripper.ungrip"],
        provides_state=True, methods={}, site="t", lab="t", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    )


@contextmanager
def _connected(name: str = "flex_remote") -> Iterator[None]:
    """An orca-client has registered the device, so the operator path can reach it."""
    card = _connected_card(name)
    tracker = "orca.gateway.registry.connection_tracker.DeviceConnectionTracker"
    with patch(f"{tracker}.peek_snapshot", return_value=card), patch(
        f"{tracker}.get_device", new=AsyncMock(return_value=card),
    ):
        yield


async def test_device_command_offline_is_a_batch_error_naming_it_not_500(client: AsyncClient) -> None:
    """The operator path raises DeviceError subclasses. A device bridge that dropped its socket
    must reach the operator named, never as an unexplained 500."""
    with _connected(), patch(
        "orca.gateway.operator_commands.OperatorDeviceCommands.run_named",
        new=AsyncMock(side_effect=DeviceOfflineError("device bridge went away")),
    ):
        resp = await client.post("/api/devices/flex_remote/commands", json=_one("gripper.ungrip"))

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "batch_error"
    [result] = detail["extras"]["results"]
    assert result["error_type"] == "DeviceOfflineError"
    assert "device bridge went away" in result["error"]


async def test_device_command_timeout_is_named_apart_from_a_refusal(client: AsyncClient) -> None:
    """Same channel, different failure: a command that never came back has its
    own error type, and the operator needs to tell it apart from a refusal."""
    with _connected(), patch(
        "orca.gateway.operator_commands.OperatorDeviceCommands.run_named",
        new=AsyncMock(side_effect=CommandTimeoutError("no answer in 30s")),
    ):
        resp = await client.post("/api/devices/flex_remote/commands", json=_one("gripper.ungrip"))

    assert resp.status_code == 502, resp.text
    [result] = resp.json()["detail"]["extras"]["results"]
    assert result["error_type"] == "CommandTimeoutError"


async def test_device_execute_reaches_a_device_only_the_agent_knows(
    client: AsyncClient,
) -> None:
    """`orca device list` shows this device. Before this, the route answered
    404 for it because the topology registry had never heard of it."""
    with _connected(), _sent_through_the_runner() as sent:
        resp = await client.post("/api/devices/flex_remote/commands", json=_one("gripper.ungrip"))

    assert resp.status_code == 200, resp.text
    assert sent.await_args_list[-1].args[1] == "gripper.ungrip"


async def test_a_device_command_body_with_an_unknown_field_is_refused(client: AsyncClient) -> None:
    with _connected("shaker1"):
        resp = await client.post(
            "/api/devices/shaker1/commands", json={"commands": [{"command": "shake", "option": {"speed": 1}}]},
        )

    assert resp.status_code == 422


@pytest.mark.parametrize("command", ["initialize", "connect", "disconnect", "shaker.initialize"])
async def test_a_command_by_name_refuses_a_lifecycle_command_and_names_its_operation(client: AsyncClient, command: str) -> None:
    """A lifecycle command by name would skip the deck follow-up its own Operation runs after it."""
    with _connected("shaker1"), _sent_through_the_runner() as sent:
        resp = await client.post("/api/devices/shaker1/commands", json=_one(command))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "validation_error"
    lifecycle = command.rpartition(".")[2]
    assert f"/api/devices/shaker1/{lifecycle}" in detail["message"]
    assert f"orca device {lifecycle} shaker1" in detail["message"]
    assert detail["extras"]["send_with"] == {
        "rest": f"POST /api/devices/{{device_id}}/{lifecycle}",
        "cli": f"orca device {lifecycle} shaker1",
    }
    sent.assert_not_awaited()


@pytest.mark.parametrize("verb", ["initialize", "connect", "disconnect"])
def test_the_cli_verb_the_refusal_names_exists(verb: str) -> None:
    assert verb in {command.name for command in device_cli.registered_commands}


async def test_a_batch_refuses_a_lifecycle_command_before_sending_anything(client: AsyncClient) -> None:
    with _connected("shaker1"), _sent_through_the_runner() as sent:
        resp = await client.post(
            "/api/devices/shaker1/commands",
            json={"commands": [{"command": "initialize"}]},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "validation_error"
    assert "orca device initialize shaker1" in detail["message"]
    sent.assert_not_awaited()


async def test_a_batch_with_a_lifecycle_name_later_on_runs_nothing(client: AsyncClient) -> None:
    with _connected("shaker1"), _sent_through_the_runner() as sent:
        resp = await client.post(
            "/api/devices/shaker1/commands",
            json={"commands": [{"command": "open"}, {"command": "initialize"}]},
        )
    assert resp.status_code == 422, resp.text
    sent.assert_not_awaited()
