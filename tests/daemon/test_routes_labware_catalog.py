"""Daemon labware-catalog routes -- operator CRUD through the deployment layer.

The catalog goes through the SAME catalog facade a hosted deployment uses, now exposed by the
deployment-registries layer (in front of the runtime) so it works with no
system mounted. The daemon defaults to the in-memory seed store. Proves the
facade policy (read-only-seed, conflict, 404, geometry validation) surfaces
correctly over HTTP on the local backend, both with and without a runtime.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


def _plate_geometry(labware_type: str) -> dict:
    return {
        "category": "plate",
        "labware_type": labware_type,
        "display_name": labware_type,
        "vendor": None,
        "plr_class_name": None,
        "num_rows": 1,
        "num_cols": 1,
        "size_x": 1.0,
        "size_y": 1.0,
        "size_z": 1.0,
        "wells": [],
    }


@pytest_asyncio.fixture
async def catalog_client() -> AsyncIterator[AsyncClient]:
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system)  # defaults to in-memory seed catalog store
    await rt.start()
    app = create_app(initial_system_runtime=rt)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://daemon.test",
        ) as c:
            yield c
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


async def test_list_returns_seed_rows(catalog_client: AsyncClient) -> None:
    resp = await catalog_client.get("/labware")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["labware"]
    assert len(rows) > 0
    assert all(r["source"] == "plr_seed" for r in rows)


async def test_list_omits_geometry_but_get_includes_it(
    catalog_client: AsyncClient,
) -> None:
    """List returns identity + facets only; geometry is fetched per row via get.

    A 384-well plate's geometry blob is ~77 KB, so a full-catalog list of
    geometry blobs runs to megabytes -- past the MCP 1 MB result cap. The list
    surface is for discovery; geometry comes from the per-row detail route.
    """
    rows = (await catalog_client.get("/labware")).json()["labware"]
    assert rows, "expected seed rows"
    assert all("geometry" not in r for r in rows)
    a_type = rows[0]["labware_type"]
    detail = (await catalog_client.get(f"/labware/{a_type}")).json()
    assert detail["geometry"], "detail route must carry the geometry blob"


async def test_add_get_delete_custom_row(catalog_client: AsyncClient) -> None:
    body = {
        "labware_type": "custom_plate",
        "display_name": "Custom Plate",
        "category": "plate",
        "vendor": None,
        "plr_class_name": None,
        "geometry": _plate_geometry("custom_plate"),
    }
    resp = await catalog_client.post("/labware", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["source"] == "operator_custom"

    got = await catalog_client.get("/labware/custom_plate")
    assert got.status_code == 200
    assert got.json()["display_name"] == "Custom Plate"

    deleted = await catalog_client.delete("/labware/custom_plate")
    assert deleted.status_code == 204
    assert (await catalog_client.get("/labware/custom_plate")).status_code == 404


async def test_geometry_set_via_post_and_put_round_trips(
    catalog_client: AsyncClient,
) -> None:
    """Geometry SET on POST/PUT is retrievable through the per-row GET route.

    The list dropped geometry; this proves the detail route still carries it and
    that operator writes (create + update) persist the geometry blob.
    """
    created = _plate_geometry("RoundTrip_demo")
    created["size_x"] = 111.0
    post = await catalog_client.post("/labware", json={
        "labware_type": "RoundTrip_demo",
        "display_name": "Round trip",
        "category": "plate",
        "geometry": created,
    })
    assert post.status_code == 201, post.text
    assert post.json()["geometry"]["size_x"] == 111.0

    got = await catalog_client.get("/labware/RoundTrip_demo")
    assert got.status_code == 200
    assert got.json()["geometry"]["size_x"] == 111.0

    updated = _plate_geometry("RoundTrip_demo")
    updated["size_x"] = 222.0
    put = await catalog_client.put("/labware/RoundTrip_demo", json={
        "display_name": "Round trip v2",
        "category": "plate",
        "geometry": updated,
    })
    assert put.status_code == 200, put.text
    assert put.json()["geometry"]["size_x"] == 222.0

    got_again = await catalog_client.get("/labware/RoundTrip_demo")
    assert got_again.json()["geometry"]["size_x"] == 222.0


async def test_add_duplicate_is_409(catalog_client: AsyncClient) -> None:
    body = {
        "labware_type": "dup",
        "display_name": "Dup",
        "category": "plate",
        "geometry": _plate_geometry("dup"),
    }
    assert (await catalog_client.post("/labware", json=body)).status_code == 201
    assert (await catalog_client.post("/labware", json=body)).status_code == 409


async def test_add_invalid_geometry_is_422(catalog_client: AsyncClient) -> None:
    body = {
        "labware_type": "bad",
        "display_name": "Bad",
        "category": "plate",
        "geometry": {"category": "plate"},  # missing required plate fields
    }
    assert (await catalog_client.post("/labware", json=body)).status_code == 422


async def test_seed_row_is_read_only(catalog_client: AsyncClient) -> None:
    seed_type = (await catalog_client.get("/labware")).json()["labware"][0][
        "labware_type"
    ]
    resp = await catalog_client.delete(f"/labware/{seed_type}")
    assert resp.status_code == 409, resp.text


async def test_get_missing_is_404(catalog_client: AsyncClient) -> None:
    assert (await catalog_client.get("/labware/nope")).status_code == 404


@pytest_asyncio.fixture
async def empty_daemon_client() -> AsyncIterator[AsyncClient]:
    """A fresh daemon with NO system mounted (the cold-start case)."""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as c:
        yield c


async def test_catalog_crud_works_with_no_system_mounted(
    empty_daemon_client: AsyncClient,
) -> None:
    """The catalog is deployment config: reachable before any topology mounts."""
    listed = await empty_daemon_client.get("/labware")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["labware"]) > 0

    body = {
        "labware_type": "cold_start_plate",
        "display_name": "Cold Start Plate",
        "category": "plate",
        "geometry": _plate_geometry("cold_start_plate"),
    }
    created = await empty_daemon_client.post("/labware", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["source"] == "operator_custom"

    got = await empty_daemon_client.get("/labware/cold_start_plate")
    assert got.status_code == 200


async def test_access_configs_list_works_with_no_system_mounted(
    empty_daemon_client: AsyncClient,
) -> None:
    resp = await empty_daemon_client.get("/access-configs")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_a_missing_type_reads_as_a_sentence_on_every_verb(
    catalog_client: AsyncClient,
) -> None:
    """`LabwareNotFound` subclasses `KeyError`, so `str()` on it is a repr.

    Read that way the operator gets their message wrapped in a second pair of
    quotes. Get, put and delete all answer for a type nobody added, and all
    three used to.
    """
    missing = "no_such_labware_type"
    put_body = {
        "display_name": "Nope",
        "category": "plate",
        "geometry": _plate_geometry(missing),
    }

    answers = [
        ("get", await catalog_client.get(f"/labware/{missing}")),
        ("put", await catalog_client.put(f"/labware/{missing}", json=put_body)),
        ("delete", await catalog_client.delete(f"/labware/{missing}")),
    ]

    for verb, resp in answers:
        assert resp.status_code == 404, f"{verb}: {resp.text}"
        detail = resp.json()["detail"]
        assert isinstance(detail, str), f"{verb}: {detail!r}"
        assert not detail.startswith(('"', "'")), (
            f"{verb} handed the operator a repr, not a message: {detail!r}"
        )
        assert missing in detail, f"{verb} does not name the type: {detail!r}"
