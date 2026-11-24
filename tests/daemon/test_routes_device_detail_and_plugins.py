"""Tests for the device-detail and plugin-read routes.

What these catch:
- Gating on /devices/{id} and /plugins.
- Unknown device name -> 404.
- Plugin list returns [] on a fresh system (no plugins registered); not 500.
- A plugin row carries only the plugin's type name.
"""

from httpx import AsyncClient

from orca.plugins.base import OrcaPlugin
from orca.runtime.system_runtime import SystemRuntime


class _ListedPlugin(OrcaPlugin):
    pass


# -- Gating ------------------------------------------------------------------


async def test_device_info_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/api/devices/shaker1")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"


async def test_plugins_list_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/api/plugins")
    assert resp.status_code == 503


# -- Wire-up -----------------------------------------------------------------


async def test_device_info_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/devices/nonexistent_device")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "device_not_found"
    assert resp.json()["detail"]["extras"]["device_id"] == "nonexistent_device"


async def test_device_info_known_returns_the_engines_state(
    client: AsyncClient,
) -> None:
    """Fixture system has shaker1; its registry entry carries what the engine holds for it."""
    resp = await client.get("/api/devices/shaker1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "shaker1"
    assert body["engine"]["type_name"]
    assert "is_busy" in body["engine"]


async def test_plugins_list_succeeds_on_fresh_system(
    client: AsyncClient,
) -> None:
    """Fresh system has no registered plugins; list returns []."""
    resp = await client.get("/api/plugins")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_a_plugin_row_carries_only_its_type_name(
    client: AsyncClient, runtime: SystemRuntime,
) -> None:
    runtime.register_plugin(_ListedPlugin())
    resp = await client.get("/api/plugins")
    assert resp.status_code == 200
    assert resp.json() == [{"type_name": "_ListedPlugin"}]
