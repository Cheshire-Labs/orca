"""Deployment profile routes on the daemon, with no system mounted.

A profile is setup: it is a file in the package folder, so it is editable before
anything is mounted.
"""

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app


async def test_a_profile_round_trips_with_no_system_mounted(empty_client: AsyncClient) -> None:
    body = {
        "name": "p1",
        "description": "round trip",
        "variables": {"speed": 500, "global.retries": 3},
        "computed": {"derived": "speed * 2"},
    }

    created = await empty_client.post("/api/profiles", json=body)
    assert created.status_code == 201, created.text
    assert created.json() == body

    got = await empty_client.get("/api/profiles/p1")
    assert got.status_code == 200
    assert got.json() == body


async def test_the_list_holds_what_was_created(empty_client: AsyncClient) -> None:
    await empty_client.post("/api/profiles", json={"name": "a"})

    listed = await empty_client.get("/api/profiles")

    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()] == ["a"]


async def test_an_unknown_profile_answers_not_found(empty_client: AsyncClient) -> None:
    got = await empty_client.get("/api/profiles/ghost")

    assert got.status_code == 404
    assert got.json()["detail"]["code"] == "profile_not_found"


async def test_a_taken_name_answers_already_exists(empty_client: AsyncClient) -> None:
    await empty_client.post("/api/profiles", json={"name": "dup"})

    again = await empty_client.post("/api/profiles", json={"name": "dup"})

    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "already_exists"


async def test_update_replaces_the_body(empty_client: AsyncClient) -> None:
    await empty_client.post("/api/profiles", json={"name": "p", "variables": {"speed": 1}})

    updated = await empty_client.put(
        "/api/profiles/p", json={"description": "new", "variables": {"speed": 9}},
    )

    assert updated.status_code == 200, updated.text
    got = (await empty_client.get("/api/profiles/p")).json()
    assert got == {"name": "p", "description": "new", "variables": {"speed": 9}, "computed": {}}


async def test_update_refuses_a_name_in_the_body(empty_client: AsyncClient) -> None:
    await empty_client.post("/api/profiles", json={"name": "p"})

    renamed = await empty_client.put("/api/profiles/p", json={"name": "other", "variables": {}})

    assert renamed.status_code == 422


async def test_updating_an_unknown_profile_answers_not_found(empty_client: AsyncClient) -> None:
    updated = await empty_client.put("/api/profiles/ghost", json={"variables": {}})

    assert updated.status_code == 404


async def test_delete_removes_the_profile(empty_client: AsyncClient) -> None:
    await empty_client.post("/api/profiles", json={"name": "p"})

    deleted = await empty_client.delete("/api/profiles/p")
    assert deleted.status_code == 204

    assert (await empty_client.get("/api/profiles/p")).status_code == 404
    assert (await empty_client.delete("/api/profiles/p")).status_code == 404


async def test_a_mistyped_field_is_refused(empty_client: AsyncClient) -> None:
    created = await empty_client.post("/api/profiles", json={"name": "p", "bogus": 1})

    assert created.status_code == 422


async def test_a_profile_outlives_the_daemon_that_wrote_it(tmp_path: Path) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(package_root=tmp_path)), base_url="http://daemon.test",
    ) as client:
        (await client.post("/api/profiles", json={"name": "keep", "variables": {"x": 1}})).raise_for_status()

    async with AsyncClient(
        transport=ASGITransport(app=create_app(package_root=tmp_path)), base_url="http://daemon.test",
    ) as next_daemon:
        got = await next_daemon.get("/api/profiles/keep")

    assert got.json()["variables"] == {"x": 1}
    written = json.loads((tmp_path / "profiles" / "keep.json").read_text(encoding="utf-8"))
    assert written["variables"] == {"x": 1}
