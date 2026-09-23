"""Tests for the device-detail and plugin-read routes.

What these catch:
- Gating on /devices/{name}, /plugins, /plugins/commands.
- Unknown device name -> 404.
- Plugin list returns [] on a fresh system (no plugins registered); not 500.
"""

from httpx import AsyncClient


# -- Gating ------------------------------------------------------------------


async def test_device_info_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/devices/shaker1")
    assert resp.status_code == 409


async def test_plugins_list_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/plugins")
    assert resp.status_code == 409


async def test_plugins_commands_gated(empty_client: AsyncClient) -> None:
    resp = await empty_client.get("/plugins/commands")
    assert resp.status_code == 409


# -- Wire-up -----------------------------------------------------------------


async def test_device_info_unknown_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.get("/devices/nonexistent_device")
    assert resp.status_code == 404


async def test_device_info_known_returns_snapshot(
    client: AsyncClient,
) -> None:
    """Fixture system has shaker1; detail endpoint should return it."""
    resp = await client.get("/devices/shaker1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "shaker1"


async def test_plugins_list_succeeds_on_fresh_system(
    client: AsyncClient,
) -> None:
    """Fresh system has no registered plugins; list returns []."""
    resp = await client.get("/plugins")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_plugins_commands_succeeds_on_fresh_system(
    client: AsyncClient,
) -> None:
    resp = await client.get("/plugins/commands")
    assert resp.status_code == 200
    assert resp.json() == []
