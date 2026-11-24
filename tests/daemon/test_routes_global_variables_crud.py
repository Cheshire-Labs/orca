"""Create, read, update and delete a global variable over REST, declared in `global_variables.json` or not."""
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.global_variables_daemon import PLATE_COUNT, a_daemon_declaring

URL = "/api/variables/global"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with a_daemon_declaring(PLATE_COUNT) as app:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon.test") as c:
            yield c


async def _read(client: AsyncClient, name: str) -> tuple[int | str | None, str]:
    resp = await client.get(f"{URL}/{name}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["value"], body["source"]


async def test_a_declared_global_reads_its_default_before_anything_is_set(client: AsyncClient) -> None:
    assert await _read(client, "plate_count") == (1, "global-default")


async def test_a_declared_global_is_set_updated_and_deleted_back_to_its_default(client: AsyncClient) -> None:
    assert (await client.put(f"{URL}/plate_count", json={"value": 2})).status_code == 200
    assert await _read(client, "plate_count") == (2, "global")

    assert (await client.put(f"{URL}/plate_count", json={"value": 3})).status_code == 200
    assert await _read(client, "plate_count") == (3, "global")

    deleted = await client.delete(f"{URL}/plate_count")
    assert deleted.status_code == 200 and deleted.json()["existed"] is True
    assert await _read(client, "plate_count") == (1, "global-default")


async def test_a_value_outside_the_declared_bounds_is_refused_and_the_value_is_kept(client: AsyncClient) -> None:
    await client.put(f"{URL}/plate_count", json={"value": 2})

    for refused in (9, 0, "two"):
        resp = await client.put(f"{URL}/plate_count", json={"value": refused})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "validation_error"

    assert await _read(client, "plate_count") == (2, "global")


async def test_an_undeclared_global_is_created_read_updated_and_deleted(client: AsyncClient) -> None:
    assert (await client.get(f"{URL}/operator")).status_code == 404

    assert (await client.put(f"{URL}/operator", json={"value": "ana"})).status_code == 200
    assert await _read(client, "operator") == ("ana", "global")

    assert (await client.put(f"{URL}/operator", json={"value": "ben"})).status_code == 200
    assert await _read(client, "operator") == ("ben", "global")

    deleted = await client.delete(f"{URL}/operator")
    assert deleted.status_code == 200 and deleted.json()["existed"] is True
    assert (await client.get(f"{URL}/operator")).status_code == 404
    assert (await client.delete(f"{URL}/operator")).json()["existed"] is False
