"""Daemon routes for a position's labware context.

``taught_with`` says what a position was jogged to; the per-labware overrides
are the narrowest layer a move resolves through. Both ride on the teachpoint,
so a coordinate edit must not quietly discard them.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import JsonValue

from orca.daemon.app import create_app

from tests.test_system_runtime import _build_simple_system
from tests.memory_activity import memory_daemon_system


SLASHED = "flex_1/B4-slot"

_CARTESIAN_COORDS = {
    "x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0,
}


def _access_config_body(name: str = "vert_a") -> dict:
    return {
        "name": name, "access_type": "vertical", "gripper_offset": 20.0,
        "vertical_clearance": 20.0, "horizontal_clearance": 100.0,
    }


CREATE_ON_ROBOT1 = "/api/teachpoints/robot1"


def _create_body(position_id: str = "hotel_3", *, reason: str | None = None, **over: JsonValue) -> dict[str, JsonValue]:
    row: dict[str, JsonValue] = {
        "position_id": position_id,
        "coord_type": "cartesian",
        "coords": dict(_CARTESIAN_COORDS),
        "access_config_name": "vert_a",
        "orientation": "right",
    }
    row.update(over)
    return {"teachpoints": [row], "reason": reason}


@pytest_asyncio.fixture
async def tp_client() -> AsyncIterator[AsyncClient]:
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
            await c.post(CREATE_ON_ROBOT1, json=_create_body())
            await c.post(CREATE_ON_ROBOT1, json=_create_body(SLASHED))
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


async def test_a_new_position_treats_every_labware_the_same(
    tp_client: AsyncClient,
) -> None:
    got = await tp_client.get("/api/teachpoints/robot1/hotel_3")

    assert got.status_code == 200, got.text
    assert got.json()["by_labware"] == {}
    assert got.json()["taught_with"] is None


async def test_create_records_what_the_position_was_taught_with(
    tp_client: AsyncClient,
) -> None:
    created = await tp_client.post(
        CREATE_ON_ROBOT1,
        json=_create_body("nest_1", taught_with="costar_96"),
    )

    assert created.status_code == 201, created.text
    got = await tp_client.get("/api/teachpoints/robot1/nest_1")
    assert got.json()["taught_with"] == "costar_96"


async def test_an_override_names_one_labware_at_one_position(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3",
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
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3", json=_override(45.0),
    )

    updated = await tp_client.put(
        "/api/teachpoints/robot1/hotel_3",
        json={"coords": {**_CARTESIAN_COORDS, "z": 9.0}},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["by_labware"] == {"deep_well": {"clearance": 45.0}}


async def test_taught_with_survives_a_coordinate_edit(
    tp_client: AsyncClient,
) -> None:
    await tp_client.put(
        "/api/teachpoints/robot1/hotel_3",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "taught_with": "costar_96",
            "update_taught_with": True,
        },
    )

    updated = await tp_client.put(
        "/api/teachpoints/robot1/hotel_3",
        json={"coords": {**_CARTESIAN_COORDS, "z": 9.0}},
    )

    assert updated.json()["taught_with"] == "costar_96"


async def test_taught_with_changes_only_when_the_flag_says_so(
    tp_client: AsyncClient,
) -> None:
    """Same shape as gateway and orientation: the value alone is not consent,
    so a client that always sends the field cannot blank it by accident."""
    await tp_client.put(
        "/api/teachpoints/robot1/hotel_3",
        json={
            "coords": dict(_CARTESIAN_COORDS),
            "taught_with": "costar_96",
            "update_taught_with": True,
        },
    )

    resp = await tp_client.put(
        "/api/teachpoints/robot1/hotel_3",
        json={"coords": dict(_CARTESIAN_COORDS), "taught_with": "deep_well"},
    )

    assert resp.json()["taught_with"] == "costar_96"


async def test_a_second_override_edit_merges(tp_client: AsyncClient) -> None:
    await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3", json=_override(45.0),
    )

    resp = await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3",
        json={"set": {"z_offset": 2.5}},
    )

    assert resp.json()["by_labware"]["deep_well"] == {
        "clearance": 45.0, "z_offset": 2.5,
    }


async def test_clearing_the_last_field_drops_the_override(
    tp_client: AsyncClient,
) -> None:
    await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3", json=_override(45.0),
    )

    resp = await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3",
        json={"clear": ["clearance"]},
    )

    assert resp.json()["by_labware"] == {}


async def test_delete_returns_the_labware_to_the_position_default(
    tp_client: AsyncClient,
) -> None:
    await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3", json=_override(45.0),
    )

    resp = await tp_client.delete(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3",
    )

    assert resp.status_code == 204, resp.text
    got = await tp_client.get("/api/teachpoints/robot1/hotel_3")
    assert got.json()["by_labware"] == {}


async def test_deleting_an_override_that_was_never_set_is_a_404(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.delete(
        "/api/teachpoint-overrides/robot1/never_excepted/hotel_3",
    )

    assert resp.status_code == 404, resp.text


async def test_an_override_on_an_unknown_position_is_a_404(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/nowhere", json=_override(45.0),
    )

    assert resp.status_code == 404, resp.text


async def test_a_field_that_is_not_a_move_parameter_is_refused(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.patch(
        "/api/teachpoint-overrides/robot1/deep_well/hotel_3",
        json={"clear": ["grip_height"]},
    )

    assert resp.status_code == 422, resp.text


async def test_an_override_on_a_slashed_position_can_be_set(
    tp_client: AsyncClient,
) -> None:
    resp = await tp_client.patch(
        f"/api/teachpoint-overrides/robot1/deep_well/{SLASHED}", json=_override(45.0),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["by_labware"] == {"deep_well": {"clearance": 45.0}}


async def test_an_override_on_a_slashed_position_can_be_cleared(
    tp_client: AsyncClient,
) -> None:
    await tp_client.patch(
        f"/api/teachpoint-overrides/robot1/deep_well/{SLASHED}", json=_override(45.0),
    )

    resp = await tp_client.delete(f"/api/teachpoint-overrides/robot1/deep_well/{SLASHED}")

    assert resp.status_code == 204, resp.text
    got = await tp_client.get(f"/api/teachpoints/robot1/{SLASHED}")
    assert got.status_code == 200, "the clear must not delete the teachpoint itself"
    assert got.json()["by_labware"] == {}
