"""Daemon write routes for the per-transporter teachpoint registry.

Mirrors the read routes already in ``orca.daemon.routes`` and the
access-config / deck-layout write pattern. Teachpoints live on a mounted
system's transporter store; access-config names are resolved through the
deployment registries. Proves happy-path create/update/delete plus the
error mappings: bad coords -> 400, unknown access-config name -> 400,
unknown device -> 404, duplicate -> 409, update/delete miss -> 404.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


_CARTESIAN_COORDS = {
    "x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0,
}
_JOINT_COORDS = {
    "rail": 5.0, "base": 10.0, "shoulder": 20.0,
    "elbow": 30.0, "wrist": 40.0, "gripper": 0.0,
}


def _access_config_body(name: str = "vert_a") -> dict:
    return {
        "name": name, "access_type": "vertical", "gripper_offset": 20.0,
        "vertical_clearance": 20.0, "horizontal_clearance": 100.0,
    }


def _create_body(position_id: str = "slot_x", **over: object) -> dict:
    body: dict = {
        "device_id": "robot1",
        "position_id": position_id,
        "coord_type": "cartesian",
        "coords": dict(_CARTESIAN_COORDS),
        "access_config_name": "vert_a",
        "orientation": "right",
    }
    body.update(over)
    return body


@pytest_asyncio.fixture
async def tp_client() -> AsyncIterator[AsyncClient]:
    """Daemon with a mounted system carrying transporter ``robot1`` whose
    teachpoint store is writable, plus a seeded ``vert_a`` access config."""
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system)
    await rt.start()
    app = create_app(initial_system_runtime=rt)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://daemon.test",
        ) as c:
            await c.post("/access-configs", json=_access_config_body())
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


# -- create ------------------------------------------------------------------


async def test_create_get_delete_roundtrip(tp_client: AsyncClient) -> None:
    created = await tp_client.post("/teachpoints", json=_create_body())
    assert created.status_code == 201, created.text
    assert created.json()["position_id"] == "slot_x"
    assert created.json()["coord_type"] == "cartesian"
    assert created.json()["access_config_name"] == "vert_a"

    got = await tp_client.get("/teachpoints/robot1/slot_x")
    assert got.status_code == 200
    assert got.json()["coords"]["x"] == 1.0

    deleted = await tp_client.delete("/teachpoints/robot1/slot_x")
    assert deleted.status_code == 204
    assert (
        await tp_client.get("/teachpoints/robot1/slot_x")
    ).status_code == 404


async def test_create_joint_without_access(tp_client: AsyncClient) -> None:
    body = _create_body(
        position_id="wp1", coord_type="joint", coords=dict(_JOINT_COORDS),
        access_config_name=None, orientation=None,
    )
    created = await tp_client.post("/teachpoints", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["coord_type"] == "joint"
    assert created.json()["access_config_name"] is None


async def test_create_cartesian_without_orientation_is_422(
    tp_client: AsyncClient,
) -> None:
    body = _create_body(position_id="bad")
    del body["orientation"]
    resp = await tp_client.post("/teachpoints", json=body)
    assert resp.status_code == 422, resp.text


async def test_create_bad_coords_is_400(tp_client: AsyncClient) -> None:
    body = _create_body(position_id="bad2", coords={"x": 1.0, "y": 2.0})
    resp = await tp_client.post("/teachpoints", json=body)
    assert resp.status_code == 400, resp.text


async def test_create_nested_type_is_400(tp_client: AsyncClient) -> None:
    body = _create_body(
        position_id="bad3", coords={**_CARTESIAN_COORDS, "type": "cartesian"},
    )
    resp = await tp_client.post("/teachpoints", json=body)
    assert resp.status_code == 400, resp.text


async def test_create_unknown_access_config_is_400(
    tp_client: AsyncClient,
) -> None:
    body = _create_body(position_id="bad4", access_config_name="ghost")
    resp = await tp_client.post("/teachpoints", json=body)
    assert resp.status_code == 400, resp.text


async def test_create_unknown_device_is_404(tp_client: AsyncClient) -> None:
    body = _create_body(device_id="ghost")
    resp = await tp_client.post("/teachpoints", json=body)
    assert resp.status_code == 404, resp.text


async def test_create_duplicate_is_409(tp_client: AsyncClient) -> None:
    assert (
        await tp_client.post("/teachpoints", json=_create_body("dup"))
    ).status_code == 201
    assert (
        await tp_client.post("/teachpoints", json=_create_body("dup"))
    ).status_code == 409


# -- update ------------------------------------------------------------------


async def test_update_replaces_coords(tp_client: AsyncClient) -> None:
    assert (
        await tp_client.post("/teachpoints", json=_create_body("u1"))
    ).status_code == 201

    new_coords = {**_CARTESIAN_COORDS, "x": 99.0}
    resp = await tp_client.put(
        "/teachpoints/robot1/u1", json={"coords": new_coords},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["coords"]["x"] == 99.0
    # access/orientation preserved from existing when flags not set
    assert resp.json()["access_config_name"] == "vert_a"
    assert resp.json()["orientation"] == "right"


async def test_update_invalid_orientation_is_422(tp_client: AsyncClient) -> None:
    assert (
        await tp_client.post("/teachpoints", json=_create_body("u_inv"))
    ).status_code == 201
    resp = await tp_client.put(
        "/teachpoints/robot1/u_inv",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "orientation": "middle",
            "update_orientation": True,
        },
    )
    assert resp.status_code == 422, resp.text


async def test_update_orientation_flag_without_value_is_422(
    tp_client: AsyncClient,
) -> None:
    assert (
        await tp_client.post("/teachpoints", json=_create_body("u_null"))
    ).status_code == 201
    resp = await tp_client.put(
        "/teachpoints/robot1/u_null",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "update_orientation": True,
        },
    )
    assert resp.status_code == 422, resp.text


async def test_update_orientation_left_right_accepted(
    tp_client: AsyncClient,
) -> None:
    assert (
        await tp_client.post("/teachpoints", json=_create_body("u_ok"))
    ).status_code == 201
    resp = await tp_client.put(
        "/teachpoints/robot1/u_ok",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "orientation": "left",
            "update_orientation": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["orientation"] == "left"


async def test_update_unknown_is_404(tp_client: AsyncClient) -> None:
    resp = await tp_client.put(
        "/teachpoints/robot1/ghost", json={"coords": dict(_CARTESIAN_COORDS)},
    )
    assert resp.status_code == 404, resp.text


async def test_update_unknown_device_is_404(tp_client: AsyncClient) -> None:
    resp = await tp_client.put(
        "/teachpoints/ghost/x", json={"coords": dict(_CARTESIAN_COORDS)},
    )
    assert resp.status_code == 404, resp.text


# -- delete ------------------------------------------------------------------


async def test_delete_missing_is_404(tp_client: AsyncClient) -> None:
    resp = await tp_client.delete("/teachpoints/robot1/nope")
    assert resp.status_code == 404, resp.text


async def test_delete_unknown_device_is_404(tp_client: AsyncClient) -> None:
    resp = await tp_client.delete("/teachpoints/ghost/x")
    assert resp.status_code == 404, resp.text


async def test_a_teachpoint_named_for_a_deck_slot_survives_the_url(
    tp_client: AsyncClient,
) -> None:
    """Deck-slot position ids are namespaced by their device, so most real
    teachpoints carry a slash: ``lh_1/carrier-7-0``. A plain ``{name}`` path
    parameter never matches one -- the slash is a path separator, the URL gains
    a segment, and read, update and delete all answer 404.

    That broke the documented way out of a bad teachpoint. One naming an
    unregistered location fails the NEXT build and 503s the deployment, and the
    recovery is deleting it through this same DELETE; on the bench it had to be
    done with raw SQL instead.
    """
    slotted = "lh_1/carrier-7-0"
    created = await tp_client.post("/teachpoints", json=_create_body(slotted))
    assert created.status_code == 201, created.text

    got = await tp_client.get(f"/teachpoints/robot1/{slotted}")
    assert got.status_code == 200, got.text
    assert got.json()["position_id"] == slotted

    updated = await tp_client.put(
        f"/teachpoints/robot1/{slotted}",
        json={"coords": {**_CARTESIAN_COORDS, "x": 42.0}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["coords"]["x"] == 42.0

    deleted = await tp_client.delete(f"/teachpoints/robot1/{slotted}")
    assert deleted.status_code == 204, deleted.text
    assert (
        await tp_client.get(f"/teachpoints/robot1/{slotted}")
    ).status_code == 404
