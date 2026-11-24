"""Creating teachpoints in bulk, and reading them back as rows that create as they are, on a mounted system."""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from pydantic import JsonValue

from orca.daemon.app import create_app

from tests.memory_activity import memory_daemon_system
from tests.test_system_runtime import _build_simple_system

_COORDS: dict[str, JsonValue] = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0}


def _row(position_id: str, **over: JsonValue) -> dict[str, JsonValue]:
    return {
        "position_id": position_id, "coord_type": "cartesian", "coords": dict(_COORDS),
        "orientation": "right", **over,
    }


@pytest_asyncio.fixture
async def tp_client() -> AsyncIterator[AsyncClient]:
    system, _ = await _build_simple_system()
    injected = await memory_daemon_system(system)
    rt = injected.runtime
    await rt.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(injected=injected)),
            base_url="http://daemon.test",
        ) as c:
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


async def _create(
    client: AsyncClient, rows: list[JsonValue], overwrite: bool = False, device_id: str = "robot1",
) -> Response:
    return await client.post(f"/api/teachpoints/{device_id}", json={"teachpoints": rows, "overwrite": overwrite})


async def _created(client: AsyncClient, rows: list[JsonValue], overwrite: bool = False) -> dict[str, JsonValue]:
    response = await _create(client, rows, overwrite)
    assert response.status_code == 201, response.text
    body: dict[str, JsonValue] = response.json()
    return body


async def test_a_create_saves_what_each_row_says(tp_client: AsyncClient) -> None:
    result = await _created(tp_client, [
        _row("slot_a", taught_with="costar_96", by_labware={"costar_96": {"z_offset": 3.0}}),
        _row("slot_b"),
    ])

    assert (result["total"], result["created"], result["updated"]) == (2, 2, 0)
    got = (await tp_client.get("/api/teachpoints/robot1/slot_a")).json()
    assert got["taught_with"] == "costar_96"
    assert got["by_labware"] == {"costar_96": {"z_offset": 3.0}}


async def test_one_bad_row_saves_nothing_and_is_listed(tp_client: AsyncClient) -> None:
    response = await _create(tp_client, [_row("slot_a"), _row("slot_b", orientation="up")])

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "validation_error"
    [error] = detail["extras"]["errors"]
    assert "'slot_b'" in error
    assert (await tp_client.get("/api/teachpoints/robot1/slot_a")).status_code == 404
    assert (await tp_client.get("/api/teachpoints/robot1/slot_b")).status_code == 404


async def test_a_taken_name_is_refused_unless_overwrite_is_set(tp_client: AsyncClient) -> None:
    await _created(tp_client, [_row("slot_a")])
    moved = _row("slot_a", coords={**_COORDS, "x": 9.0})

    refused = await _create(tp_client, [moved])
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "already_exists"
    assert refused.json()["detail"]["extras"]["names"] == ["slot_a"]
    assert (await tp_client.get("/api/teachpoints/robot1/slot_a")).json()["coords"]["x"] == 1.0

    replaced = await _created(tp_client, [moved], overwrite=True)
    assert (replaced["created"], replaced["updated"]) == (0, 1)
    assert (await tp_client.get("/api/teachpoints/robot1/slot_a")).json()["coords"]["x"] == 9.0


async def test_a_listed_row_is_the_get_row_and_creates_as_it_is(tp_client: AsyncClient) -> None:
    await _created(tp_client, [_row("slot_a", taught_with="costar_96")])

    listed = await tp_client.get("/api/teachpoints", params={"device_id": "robot1"})
    assert listed.status_code == 200, listed.text
    [row] = [r for r in listed.json() if r["position_id"] == "slot_a"]
    assert row == (await tp_client.get("/api/teachpoints/robot1/slot_a")).json()

    assert (await tp_client.delete("/api/teachpoints/robot1/slot_a")).status_code == 204
    await _created(tp_client, [row])
    assert (await tp_client.get("/api/teachpoints/robot1/slot_a")).json() == row


async def test_an_unknown_device_answers_device_not_found(tp_client: AsyncClient) -> None:
    listed = await tp_client.get("/api/teachpoints", params={"device_id": "ghost"})
    created = await _create(tp_client, [_row("slot_a")], device_id="ghost")

    for response in (listed, created):
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "device_not_found"


async def test_a_device_that_is_not_a_transporter_is_refused(tp_client: AsyncClient) -> None:
    listed = await tp_client.get("/api/teachpoints", params={"device_id": "shaker1"})
    created = await _create(tp_client, [_row("slot_a")], device_id="shaker1")

    for response in (listed, created):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "validation_error"


async def test_a_row_that_is_not_an_object_refuses_the_whole_request(tp_client: AsyncClient) -> None:
    response = await _create(tp_client, [_row("slot_a"), 5])

    assert response.status_code == 422, response.text
    assert (await tp_client.get("/api/teachpoints/robot1/slot_a")).status_code == 404
