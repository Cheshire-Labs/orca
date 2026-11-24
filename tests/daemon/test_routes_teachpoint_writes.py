"""Daemon write routes for the per-transporter teachpoint registry.

Follows the access-config / deck-layout write pattern. Teachpoints live on a mounted
system's transporter store; access-config names are resolved through the
deployment registries. Proves happy-path create/update/delete plus the
error mappings: bad coords -> 422, unknown access-config name -> 422,
unknown device -> 404, duplicate -> 409, update/delete miss -> 404.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from cheshire_drivers.teachpoints import CartesianCoordinates, Teachpoint
from httpx import ASGITransport, AsyncClient, Response
from pydantic import JsonValue

from orca.daemon.app import create_app

from tests.test_system_runtime import _build_simple_system
from tests.memory_activity import memory_daemon_system
from tests.daemon.started_daemon import started_daemon


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


def _row(position_id: str = "slot_x", **over: JsonValue) -> dict[str, JsonValue]:
    row: dict[str, JsonValue] = {
        "position_id": position_id,
        "coord_type": "cartesian",
        "coords": dict(_CARTESIAN_COORDS),
        "access_config_name": "vert_a",
        "orientation": "right",
    }
    row.update(over)
    return row


async def _create(client: AsyncClient, row: dict[str, JsonValue], device_id: str = "robot1") -> Response:
    return await client.post(f"/api/teachpoints/{device_id}", json={"teachpoints": [row]})


@pytest_asyncio.fixture
async def tp_client() -> AsyncIterator[AsyncClient]:
    """Daemon with a mounted system carrying transporter ``robot1`` whose
    teachpoint store is writable, plus a seeded ``vert_a`` access config."""
    system, _ = await _build_simple_system()
    injected = await memory_daemon_system(system)
    rt = injected.runtime
    await rt.start()
    app = create_app(injected=injected)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://daemon.test",
        ) as c:
            await c.post("/api/access-configs", json=_access_config_body())
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


# -- create ------------------------------------------------------------------


async def test_create_get_delete_roundtrip(tp_client: AsyncClient) -> None:
    created = await _create(tp_client, _row())
    assert created.status_code == 201, created.text
    assert created.json()["created"] == 1

    got = await tp_client.get("/api/teachpoints/robot1/slot_x")
    assert got.status_code == 200
    assert got.json()["position_id"] == "slot_x"
    assert got.json()["coord_type"] == "cartesian"
    assert got.json()["access_config_name"] == "vert_a"
    assert got.json()["coords"]["x"] == 1.0

    deleted = await tp_client.delete("/api/teachpoints/robot1/slot_x")
    assert deleted.status_code == 204
    assert (
        await tp_client.get("/api/teachpoints/robot1/slot_x")
    ).status_code == 404


async def test_create_joint_without_access(tp_client: AsyncClient) -> None:
    row = _row(
        position_id="wp1", coord_type="joint", coords=dict(_JOINT_COORDS),
        access_config_name=None, orientation=None,
    )
    created = await _create(tp_client, row)
    assert created.status_code == 201, created.text
    got = (await tp_client.get("/api/teachpoints/robot1/wp1")).json()
    assert got["coord_type"] == "joint"
    assert got["access_config_name"] is None


async def test_create_cartesian_without_orientation_is_422(
    tp_client: AsyncClient,
) -> None:
    row = _row(position_id="bad")
    del row["orientation"]
    resp = await _create(tp_client, row)
    assert resp.status_code == 422, resp.text


async def test_create_bad_coords_is_422(tp_client: AsyncClient) -> None:
    resp = await _create(tp_client, _row(position_id="bad2", coords={"x": 1.0, "y": 2.0}))
    assert resp.status_code == 422, resp.text


async def test_create_nested_type_is_422(tp_client: AsyncClient) -> None:
    row = _row(position_id="bad3", coords={**_CARTESIAN_COORDS, "type": "cartesian"})
    resp = await _create(tp_client, row)
    assert resp.status_code == 422, resp.text


async def test_create_unknown_access_config_is_422(
    tp_client: AsyncClient,
) -> None:
    resp = await _create(tp_client, _row(position_id="bad4", access_config_name="ghost"))
    assert resp.status_code == 422, resp.text


async def test_create_unknown_device_is_404(tp_client: AsyncClient) -> None:
    resp = await _create(tp_client, _row(), device_id="ghost")
    assert resp.status_code == 404, resp.text


async def test_create_duplicate_is_409(tp_client: AsyncClient) -> None:
    assert (
        await _create(tp_client, _row("dup"))
    ).status_code == 201
    assert (
        await _create(tp_client, _row("dup"))
    ).status_code == 409


# -- update ------------------------------------------------------------------


async def test_update_replaces_coords(tp_client: AsyncClient) -> None:
    assert (
        await _create(tp_client, _row("u1"))
    ).status_code == 201

    new_coords = {**_CARTESIAN_COORDS, "x": 99.0}
    resp = await tp_client.put(
        "/api/teachpoints/robot1/u1", json={"coords": new_coords},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["coords"]["x"] == 99.0
    # access/orientation preserved from existing when flags not set
    assert resp.json()["access_config_name"] == "vert_a"
    assert resp.json()["orientation"] == "right"


async def test_update_invalid_orientation_is_refused(tp_client: AsyncClient) -> None:
    assert (
        await _create(tp_client, _row("u_inv"))
    ).status_code == 201
    resp = await tp_client.put(
        "/api/teachpoints/robot1/u_inv",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "orientation": "middle",
            "update_orientation": True,
        },
    )
    # The same request-model check as create, so the same status.
    assert resp.status_code == 422, resp.text


async def test_update_orientation_flag_without_value_is_refused(
    tp_client: AsyncClient,
) -> None:
    assert (
        await _create(tp_client, _row("u_null"))
    ).status_code == 201
    resp = await tp_client.put(
        "/api/teachpoints/robot1/u_null",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "update_orientation": True,
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "validation_error"


async def test_update_orientation_left_right_accepted(
    tp_client: AsyncClient,
) -> None:
    assert (
        await _create(tp_client, _row("u_ok"))
    ).status_code == 201
    resp = await tp_client.put(
        "/api/teachpoints/robot1/u_ok",
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
        "/api/teachpoints/robot1/ghost", json={"coords": dict(_CARTESIAN_COORDS)},
    )
    assert resp.status_code == 404, resp.text


async def test_update_unknown_device_is_404(tp_client: AsyncClient) -> None:
    resp = await tp_client.put(
        "/api/teachpoints/ghost/x", json={"coords": dict(_CARTESIAN_COORDS)},
    )
    assert resp.status_code == 404, resp.text


# -- delete ------------------------------------------------------------------


async def test_delete_missing_is_404(tp_client: AsyncClient) -> None:
    resp = await tp_client.delete("/api/teachpoints/robot1/nope")
    assert resp.status_code == 404, resp.text


async def test_delete_unknown_device_is_404(tp_client: AsyncClient) -> None:
    resp = await tp_client.delete("/api/teachpoints/ghost/x")
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
    created = await _create(tp_client, _row(slotted))
    assert created.status_code == 201, created.text

    got = await tp_client.get(f"/api/teachpoints/robot1/{slotted}")
    assert got.status_code == 200, got.text
    assert got.json()["position_id"] == slotted

    updated = await tp_client.put(
        f"/api/teachpoints/robot1/{slotted}",
        json={"coords": {**_CARTESIAN_COORDS, "x": 42.0}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["coords"]["x"] == 42.0

    deleted = await tp_client.delete(f"/api/teachpoints/robot1/{slotted}")
    assert deleted.status_code == 204, deleted.text
    assert (
        await tp_client.get(f"/api/teachpoints/robot1/{slotted}")
    ).status_code == 404


async def test_deleting_a_name_with_a_labware_segment_leaves_another_teachpoints_override(
    tp_client: AsyncClient,
) -> None:
    await _create(tp_client, _row("flex_1", by_labware={"B4": {"clearance": 45.0}}))
    await _create(tp_client, _row("flex_1/labware/B4"))

    resp = await tp_client.delete("/api/teachpoints/robot1/flex_1/labware/B4")

    assert resp.status_code == 204, resp.text
    kept = await tp_client.get("/api/teachpoints/robot1/flex_1")
    assert kept.json()["by_labware"] == {"B4": {"clearance": 45.0}}


@pytest.mark.parametrize(
    "name", ["flex_1/labware/B4", "labware/B4", "flex_1/labware", "a/labware/b/c", "flex_1/labware_hotel"],
)
async def test_a_name_with_a_labware_segment_round_trips(
    tp_client: AsyncClient, name: str,
) -> None:
    created = await _create(tp_client, _row(name))
    assert created.status_code == 201, created.text

    got = await tp_client.get(f"/api/teachpoints/robot1/{name}")
    assert got.status_code == 200, got.text
    assert got.json()["position_id"] == name

    deleted = await tp_client.delete(f"/api/teachpoints/robot1/{name}")
    assert deleted.status_code == 204, deleted.text
    assert (await tp_client.get(f"/api/teachpoints/robot1/{name}")).status_code == 404


async def test_listing_one_device_takes_it_as_a_query_parameter(tp_client: AsyncClient) -> None:
    assert (await _create(tp_client, _row())).status_code == 201

    listed = await tp_client.get("/api/teachpoints", params={"device_id": "robot1"})

    assert listed.status_code == 200, listed.text
    assert "slot_x" in [row["position_id"] for row in listed.json()]


async def test_delete_reaches_the_file_with_no_system_loaded(tmp_path: Path) -> None:
    app = create_app(package_root=tmp_path)
    store = app.state.daemon.setup.teachpoints("robot1")
    await store.add(
        Teachpoint(
            position_id="flex_1/slot",
            coordinates=CartesianCoordinates(x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=0.0, roll=0.0),
            orientation="right",
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as client:
        deleted = await client.delete("/api/teachpoints/robot1/flex_1/slot", params={"reason": "fix"})
        again = await client.delete("/api/teachpoints/robot1/flex_1/slot")

    assert deleted.status_code == 204, deleted.text
    assert await store.get("flex_1/slot") is None
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "teachpoint_not_found"


async def test_a_delete_with_no_system_loaded_writes_its_audit_row(tmp_path: Path) -> None:
    app = create_app(package_root=tmp_path)
    await app.state.daemon.setup.teachpoints("robot1").add(
        Teachpoint(
            position_id="slot",
            coordinates=CartesianCoordinates(x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=0.0, roll=0.0),
            orientation="right",
        ),
    )
    async with started_daemon(app) as client:
        deleted = await client.delete("/api/teachpoints/robot1/slot", params={"reason": "retaught"})
        assert deleted.status_code == 204, deleted.text
        rows: list[dict[str, object]] = []
        for _ in range(50):
            listed = await client.get("/api/audit", params={"action_name": "teachpoints.delete"})
            rows = listed.json()
            if rows:
                break
            await asyncio.sleep(0.02)

    [row] = rows
    assert row["reason"] == "retaught"


async def test_the_other_teachpoint_routes_need_a_system(tmp_path: Path) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(package_root=tmp_path)), base_url="http://daemon.test",
    ) as client:
        resp = await client.get("/api/teachpoints")

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "runtime_not_ready"


async def test_a_teachpoint_for_a_device_that_is_not_a_transporter_says_so(tp_client: AsyncClient) -> None:
    resp = await _create(tp_client, _row(), device_id="shaker1")

    assert resp.status_code == 422, resp.text
    assert "not a transporter" in resp.json()["detail"]["message"]
