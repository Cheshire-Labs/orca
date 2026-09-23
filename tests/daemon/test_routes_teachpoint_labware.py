"""Daemon routes for a position's labware context.

``taught_with`` says what a position was jogged to; the per-labware overrides
are the narrowest layer a move resolves through. Both ride on the teachpoint,
so a coordinate edit must not quietly discard them.
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


def _access_config_body(name: str = "vert_a") -> dict:
    return {
        "name": name, "access_type": "vertical", "gripper_offset": 20.0,
        "vertical_clearance": 20.0, "horizontal_clearance": 100.0,
    }


def _create_body(position_id: str = "hotel_3", **over: object) -> dict:
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
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system)
    await rt.start()
    app = create_app(initial_system_runtime=rt)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://daemon.test",
        ) as c:
            await c.post("/access-configs", json=_access_config_body())
            await c.post("/teachpoints", json=_create_body())
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


async def test_a_new_position_treats_every_labware_the_same(
    tp_client: AsyncClient,
) -> None:
    got = await tp_client.get("/teachpoints/robot1/hotel_3")

    assert got.status_code == 200, got.text
    assert got.json()["by_labware"] == {}
    assert got.json()["taught_with"] is None


async def test_create_records_what_the_position_was_taught_with(
    tp_client: AsyncClient,
) -> None:
    created = await tp_client.post(
        "/teachpoints",
        json=_create_body("nest_1", taught_with="costar_96"),
    )

    assert created.status_code == 201, created.text
    assert created.json()["taught_with"] == "costar_96"


async def test_an_override_names_one_labware_at_one_position(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well",
        json={"set": {"clearance": 45.0}},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["by_labware"] == {"deep_well": {"clearance": 45.0}}


def _override(clearance: float) -> dict:
    return {"set": {"clearance": clearance}}


async def test_the_override_survives_a_coordinate_edit(
    tp_client: AsyncClient,
) -> None:
    """Re-teaching coordinates is a different decision from re-measuring how a
    plate is held here. Losing the override on a jog would be silent."""
    await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well", json=_override(45.0),
    )

    updated = await tp_client.put(
        "/teachpoints/robot1/hotel_3",
        json={"coords": {**_CARTESIAN_COORDS, "z": 9.0}},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["by_labware"] == {"deep_well": {"clearance": 45.0}}


async def test_taught_with_survives_a_coordinate_edit(
    tp_client: AsyncClient,
) -> None:
    await tp_client.put(
        "/teachpoints/robot1/hotel_3",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "taught_with": "costar_96",
            "update_taught_with": True,
        },
    )

    updated = await tp_client.put(
        "/teachpoints/robot1/hotel_3",
        json={"coords": {**_CARTESIAN_COORDS, "z": 9.0}},
    )

    assert updated.json()["taught_with"] == "costar_96"


async def test_taught_with_changes_only_when_the_flag_says_so(
    tp_client: AsyncClient,
) -> None:
    """Same shape as gateway and orientation: the value alone is not consent,
    so a client that always sends the field cannot blank it by accident."""
    await tp_client.put(
        "/teachpoints/robot1/hotel_3",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "taught_with": "costar_96",
            "update_taught_with": True,
        },
    )

    resp = await tp_client.put(
        "/teachpoints/robot1/hotel_3",
        json={"coords": dict(_CARTESIAN_COORDS), "taught_with": "deep_well"},
    )

    assert resp.json()["taught_with"] == "costar_96"


async def test_a_second_override_edit_merges(tp_client: AsyncClient) -> None:
    await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well", json=_override(45.0),
    )

    resp = await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well",
        json={"set": {"z_offset": 2.5}},
    )

    assert resp.json()["by_labware"]["deep_well"] == {
        "clearance": 45.0, "z_offset": 2.5,
    }


async def test_clearing_the_last_field_drops_the_override(
    tp_client: AsyncClient,
) -> None:
    await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well", json=_override(45.0),
    )

    resp = await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well",
        json={"clear": ["clearance"]},
    )

    assert resp.json()["by_labware"] == {}


async def test_delete_returns_the_labware_to_the_position_default(
    tp_client: AsyncClient,
) -> None:
    await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well", json=_override(45.0),
    )

    resp = await tp_client.delete(
        "/teachpoints/robot1/hotel_3/labware/deep_well",
    )

    assert resp.status_code == 204, resp.text
    got = await tp_client.get("/teachpoints/robot1/hotel_3")
    assert got.json()["by_labware"] == {}


async def test_deleting_an_override_that_was_never_set_is_a_404(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.delete(
        "/teachpoints/robot1/hotel_3/labware/never_excepted",
    )

    assert resp.status_code == 404, resp.text


async def test_an_override_on_an_unknown_position_is_a_404(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.patch(
        "/teachpoints/robot1/nowhere/labware/deep_well", json=_override(45.0),
    )

    assert resp.status_code == 404, resp.text


async def test_a_field_that_is_not_a_move_parameter_is_refused(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.patch(
        "/teachpoints/robot1/hotel_3/labware/deep_well",
        json={"clear": ["grip_height"]},
    )

    assert resp.status_code == 422, resp.text
