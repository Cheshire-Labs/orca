"""Daemon write routes for the access-config and deck-layout registries.

Follows the labware-catalog CRUD pattern. Access-configs are deployment-scoped (work with
no system mounted); deck-layouts require a mounted system with a liquid
handler. Proves happy-path create/update/delete plus the error mappings the
routes choose: duplicate access-config -> 409, unknown name or device -> 404,
delete-miss -> 404.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app


def _access_config_body(name: str = "vertical_a") -> dict:
    return {
        "name": name,
        "access_type": "vertical",
        "gripper_offset": 20.0,
        "vertical_clearance": 20.0,
        "horizontal_clearance": 100.0,
    }


def _deck_layout_body() -> dict:
    return {"deck_type": "BRAVO_96", "resources": []}


@pytest_asyncio.fixture
async def empty_daemon_client() -> AsyncIterator[AsyncClient]:
    """A fresh daemon with NO system mounted (access-configs reachable)."""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as c:
        yield c


# -- access-configs ----------------------------------------------------------


async def test_access_config_create_get_delete(
    empty_daemon_client: AsyncClient,
) -> None:
    body = _access_config_body()
    created = await empty_daemon_client.post("/api/access-configs", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "vertical_a"

    got = await empty_daemon_client.get("/api/access-configs/vertical_a")
    assert got.status_code == 200
    assert got.json()["access_type"] == "vertical"

    deleted = await empty_daemon_client.delete("/api/access-configs/vertical_a")
    assert deleted.status_code == 204
    assert (
        await empty_daemon_client.get("/api/access-configs/vertical_a")
    ).status_code == 404


async def test_access_config_create_duplicate_is_409(
    empty_daemon_client: AsyncClient,
) -> None:
    body = _access_config_body("dup")
    assert (
        await empty_daemon_client.post("/api/access-configs", json=body)
    ).status_code == 201
    assert (
        await empty_daemon_client.post("/api/access-configs", json=body)
    ).status_code == 409


async def test_access_config_update_roundtrips(
    empty_daemon_client: AsyncClient,
) -> None:
    body = _access_config_body("upd")
    assert (
        await empty_daemon_client.post("/api/access-configs", json=body)
    ).status_code == 201

    updated = await empty_daemon_client.put("/api/access-configs/upd", json={"gripper_offset": 42.0})
    assert updated.status_code == 200, updated.text
    assert updated.json()["gripper_offset"] == 42.0

    got = await empty_daemon_client.get("/api/access-configs/upd")
    assert got.json()["gripper_offset"] == 42.0


async def test_access_config_update_unknown_is_404(
    empty_daemon_client: AsyncClient,
) -> None:
    resp = await empty_daemon_client.put("/api/access-configs/ghost", json={"gripper_offset": 1.0})
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "access_config_not_found"


async def test_access_config_update_refuses_a_name_in_the_body(
    empty_daemon_client: AsyncClient,
) -> None:
    resp = await empty_daemon_client.put(
        "/api/access-configs/path_name", json={"name": "body_name", "gripper_offset": 1.0},
    )
    assert resp.status_code == 422, resp.text


async def test_access_config_create_duplicate_names_the_config(
    empty_daemon_client: AsyncClient,
) -> None:
    body = _access_config_body("taken")
    await empty_daemon_client.post("/api/access-configs", json=body)
    resp = await empty_daemon_client.post("/api/access-configs", json=body)
    assert resp.json()["detail"]["code"] == "already_exists"
    assert resp.json()["detail"]["extras"] == {"name": "taken"}


async def test_access_config_delete_missing_is_404(
    empty_daemon_client: AsyncClient,
) -> None:
    resp = await empty_daemon_client.delete("/api/access-configs/nope")
    assert resp.status_code == 404, resp.text


# -- deck-layouts ------------------------------------------------------------


async def test_deck_layout_create_get_delete(
    lh_daemon_client: AsyncClient,
) -> None:
    body = _deck_layout_body()
    created = await lh_daemon_client.put(
        "/api/deck-layouts/lh1/primary", json={"config": body},
    )
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "primary"
    assert created.json()["config"]["deck_type"] == "BRAVO_96"
    assert created.json()["rebuild_note"]

    got = await lh_daemon_client.get("/api/deck-layouts/lh1/primary")
    assert got.status_code == 200

    deleted = await lh_daemon_client.delete("/api/deck-layouts/lh1/primary")
    assert deleted.status_code == 204
    assert (
        await lh_daemon_client.get("/api/deck-layouts/lh1/primary")
    ).status_code == 404


async def test_deck_layout_create_unknown_device_is_404(
    lh_daemon_client: AsyncClient,
) -> None:
    resp = await lh_daemon_client.put(
        "/api/deck-layouts/ghost/x", json={"config": _deck_layout_body()},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "liquid_handler_not_found"


async def test_deck_layout_update_roundtrips(
    lh_daemon_client: AsyncClient,
) -> None:
    assert (
        await lh_daemon_client.put(
            "/api/deck-layouts/lh1/upd", json={"config": _deck_layout_body()},
        )
    ).status_code == 200

    new_config = {"deck_type": "MULTIFLEX", "resources": []}
    updated = await lh_daemon_client.put(
        "/api/deck-layouts/lh1/upd", json={"config": new_config},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["config"]["deck_type"] == "MULTIFLEX"


async def test_deck_layout_delete_missing_is_404(
    lh_daemon_client: AsyncClient,
) -> None:
    resp = await lh_daemon_client.delete("/api/deck-layouts/lh1/nope")
    assert resp.status_code == 404, resp.text


async def test_a_deck_layout_route_needs_a_system(empty_daemon_client: AsyncClient) -> None:
    resp = await empty_daemon_client.get("/api/deck-layouts")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"
