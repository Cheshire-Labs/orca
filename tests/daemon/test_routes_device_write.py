"""Tests for device-write routes + capabilities introspection.

Covers:
- GET /devices/{name}/capabilities: lists typed capability methods the
  device's driver supports (introspected from interface membership).
- POST /devices/{name}/execute: sends a generic IGenericExecutable command.
- POST /devices/{name}/invoke: dispatches to a typed capability method.
- POST /devices/{name}/initialize: brings up + resets. Asks for no motion.
- POST /devices/{name}/connect, /disconnect: the two ends of holding a device,
  neither of which moves it.

The reason field is gone from execute and initialize (device verbs are operator
commands during a run, not audit-worthy on their own). Labware
operator-override verbs still require reason.
"""

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from orca.gateway.adhoc import AdhocResult
from orca.gateway.controller.exceptions import (
    CommandTimeoutError,
    DeviceOfflineError,
)


# -- Gating ------------------------------------------------------------------


async def test_device_capabilities_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/devices/shaker1/capabilities")
    assert resp.status_code == 409


async def test_device_execute_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/devices/shaker1/execute",
        json={"command": "noop"},
    )
    assert resp.status_code == 409


async def test_device_invoke_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post(
        "/devices/shaker1/invoke",
        json={"capability": "shake", "kwargs": {}},
    )
    assert resp.status_code == 409


async def test_device_initialize_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post("/devices/shaker1/initialize", json={})
    assert resp.status_code == 409


# -- Unknown device ---------------------------------------------------------


async def test_device_capabilities_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.get("/devices/no_such_device/capabilities")
    assert resp.status_code == 404


async def test_device_execute_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/devices/no_such_device/execute",
        json={"command": "noop"},
    )
    assert resp.status_code == 404


async def test_device_invoke_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/devices/no_such_device/invoke",
        json={"capability": "shake"},
    )
    assert resp.status_code == 404


async def test_device_invoke_accepts_bare_method_name(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/devices/shaker1/invoke",
        json={"capability": "shake", "kwargs": {"speed": 500, "duration": 1}},
    )
    assert resp.status_code == 200, resp.text


async def test_device_invoke_legacy_namespaced_form_still_works(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/devices/shaker1/invoke",
        json={"capability": "shaker.shake", "kwargs": {"speed": 500, "duration": 1}},
    )
    assert resp.status_code == 200, resp.text


async def test_device_invoke_rejects_audit_gated_initialize(
    client: AsyncClient,
) -> None:
    # `initialize` lives on Device, not on a capability interface, so it
    # has its own audit-gated verb. `invoke` must not reach it.
    resp = await client.post(
        "/devices/shaker1/invoke",
        json={"capability": "initialize", "kwargs": {}},
    )
    assert resp.status_code == 400, resp.text
    assert "initialize" in resp.text


async def test_device_invoke_rejects_private_method(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/devices/shaker1/invoke",
        json={"capability": "_private", "kwargs": {}},
    )
    assert resp.status_code == 400, resp.text


async def test_device_invoke_rejects_trailing_dot_capability(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/devices/shaker1/invoke",
        json={"capability": "shaker.", "kwargs": {}},
    )
    assert resp.status_code == 400, resp.text


async def test_device_initialize_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post("/devices/no_such_device/initialize", json={})
    assert resp.status_code == 404


# -- Capabilities -----------------------------------------------------------


async def test_device_capabilities_returns_list(client: AsyncClient) -> None:
    """The fixture's shaker1 implements UniversalMockDevice which satisfies
    several capability interfaces; the endpoint returns a list of
    CommandDescriptor DTOs."""
    resp = await client.get("/devices/shaker1/capabilities")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    for item in items:
        assert "device_name" in item
        assert "capability" in item
        assert "danger_level" in item
        assert "params" in item
        assert item["device_name"] == "shaker1"


# -- Unified device registry -------------------------------------------------


async def test_device_registry_list_returns_wrapped_payload(
    client: AsyncClient,
) -> None:
    """`GET /devices/registry` returns `{"devices": [...]}` shape mirroring
    a hosted deployment's REST surface. Each entry carries the two-card view + live
    state + per-mode eligibility."""
    resp = await client.get("/devices/registry")
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
    assert {"pure_sim", "device_sim", "live"} <= set(eligibility.keys())


async def test_device_registry_show_known_device_returns_entry(
    client: AsyncClient,
) -> None:
    resp = await client.get("/devices/registry/shaker1")
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["name"] == "shaker1"
    assert "topology_card" in entry
    assert "mode_eligibility" in entry


async def test_device_registry_show_unknown_device_404s(
    client: AsyncClient,
) -> None:
    resp = await client.get("/devices/registry/no_such_device")
    assert resp.status_code == 404


# -- Initialize --------------------------------------------------------------


async def test_device_initialize_succeeds(client: AsyncClient) -> None:
    resp = await client.post("/devices/shaker1/initialize", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "initialized"


# -- Connect / disconnect ----------------------------------------------------


async def test_device_connect_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post("/devices/shaker1/connect", json={})
    assert resp.status_code == 409


async def test_device_disconnect_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.post("/devices/shaker1/disconnect", json={})
    assert resp.status_code == 409


async def test_device_connect_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/devices/no_such_device/connect", json={})
    assert resp.status_code == 404


async def test_device_disconnect_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/devices/no_such_device/disconnect", json={})
    assert resp.status_code == 404


async def test_device_connect_succeeds(client: AsyncClient) -> None:
    resp = await client.post("/devices/shaker1/connect", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "connected"


async def test_device_disconnect_succeeds(client: AsyncClient) -> None:
    resp = await client.post("/devices/shaker1/disconnect", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "disconnected"


def _connected_card():
    """A device bridge advertising one vendor command, for a device topology
    never declared: the only shape that reaches the gateway on this route."""
    from datetime import datetime

    from orca.gateway.registry.snapshot import DeviceSnapshot

    return DeviceSnapshot(
        type="FlexLiquidHandlerDriver", name="flex_remote",
        interfaces=["ILiquidHandler"], capabilities=["gripper.ungrip"],
        provides_state=True, methods={}, site="t", lab="t", workcell=None,
        status="ready", last_seen=datetime.utcnow(),
    )


async def test_device_execute_offline_is_503_not_500(client: AsyncClient) -> None:
    """`adhoc` raises DeviceError subclasses. The route caught only KeyError,
    TypeError and ValueError, so a device bridge that dropped its socket
    reached the operator as an unexplained 500."""
    with patch(
        "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
        return_value=_connected_card(),
    ), patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(side_effect=DeviceOfflineError("device bridge went away")),
    ):
        resp = await client.post(
            "/devices/flex_remote/execute", json={"command": "gripper.ungrip"},
        )

    assert resp.status_code == 503, resp.text
    assert "device bridge went away" in resp.text


async def test_device_invoke_timeout_is_504_not_500(client: AsyncClient) -> None:
    """Same channel, different failure: a command that never came back has its
    own status, and the operator needs to tell it apart from a refusal."""
    with patch(
        "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
        return_value=_connected_card(),
    ), patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(side_effect=CommandTimeoutError("no answer in 30s")),
    ):
        resp = await client.post(
            "/devices/flex_remote/invoke", json={"capability": "gripper.ungrip"},
        )

    assert resp.status_code == 504, resp.text


async def test_device_execute_reaches_a_device_only_the_agent_knows(
    client: AsyncClient,
) -> None:
    """`orca device list` shows this device. Before this, the route answered
    404 for it because the topology registry had never heard of it."""
    with patch(
        "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
        return_value=_connected_card(),
    ), patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(return_value=AdhocResult(raw=None, interfaces=frozenset())),
    ) as sent:
        resp = await client.post(
            "/devices/flex_remote/execute", json={"command": "gripper.ungrip"},
        )

    assert resp.status_code == 200, resp.text
    assert sent.await_args.kwargs["command"] == "gripper.ungrip"
