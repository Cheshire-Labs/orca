"""Daemon routes for reading and editing an arm's move defaults.

Deployment-scoped, so the read/edit routes work with no system mounted; a
mounted system additionally puts its untuned arms in the list.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from cheshire_drivers.move_parameters import SEED_MOVE_PARAMETERS
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def empty_daemon_client() -> AsyncIterator[AsyncClient]:
    """A fresh daemon with NO system mounted."""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def mounted_daemon_client() -> AsyncIterator[AsyncClient]:
    """A daemon with a mounted system carrying one transporter."""
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system)
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


async def test_an_untuned_arm_reads_as_the_seed(empty_daemon_client) -> None:
    resp = await empty_daemon_client.get("/move-defaults/pf400")

    assert resp.status_code == 200
    body = resp.json()
    assert body["parameters"]["travel_margin"] == SEED_MOVE_PARAMETERS.travel_margin
    assert set(body["sources"].values()) == {"seed"}


async def test_a_patch_changes_the_field_it_names(empty_daemon_client) -> None:
    resp = await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"travel_margin": 25.0}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["parameters"]["travel_margin"] == 25.0
    assert body["sources"]["travel_margin"] == "defaults"
    assert body["sources"]["jaw_opening"] == "seed"


async def test_a_second_patch_does_not_restate_the_first(empty_daemon_client) -> None:
    await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"travel_margin": 25.0}},
    )

    resp = await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"jaw_opening": 18.0}},
    )

    assert resp.json()["parameters"] == {
        **SEED_MOVE_PARAMETERS.model_dump(),
        "travel_margin": 25.0,
        "jaw_opening": 18.0,
    }


async def test_clearing_a_field_hands_it_back_to_the_seed(empty_daemon_client) -> None:
    await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"travel_margin": 25.0}},
    )

    resp = await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"clear": ["travel_margin"]},
    )

    assert resp.json()["sources"]["travel_margin"] == "seed"


async def test_a_field_the_model_does_not_have_is_refused(empty_daemon_client) -> None:
    """A misspelt field silently ignored is a number the operator believes they set."""
    resp = await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"grip_height": 4.0}},
    )

    assert resp.status_code == 422


async def test_a_field_name_the_model_does_not_have_cannot_be_cleared(
    empty_daemon_client,
) -> None:
    resp = await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"clear": ["grip_height"]},
    )

    assert resp.status_code == 422


async def test_a_number_the_position_decides_is_refused(empty_daemon_client) -> None:
    """Storing it would report back a value this deployment chose that no move
    ever reads, because the teachpoint supplies that number every time."""
    resp = await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"clearance": 45.0}},
    )

    assert resp.status_code == 400
    assert "access config" in resp.json()["detail"]


async def test_a_body_with_the_wrong_outer_key_is_refused(empty_daemon_client) -> None:
    """Otherwise it answers 200 with an unchanged record, which reads exactly like
    the edit having landed."""
    resp = await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"parameters": {"travel_margin": 25.0}},
    )

    assert resp.status_code == 422


async def test_an_edit_naming_nothing_is_refused(empty_daemon_client) -> None:
    resp = await empty_daemon_client.patch("/move-defaults/pf400", json={})

    assert resp.status_code == 422


async def test_resetting_an_untuned_arm_is_a_404(empty_daemon_client) -> None:
    """Reporting success would tell an operator their tuning was discarded when
    there was none, which reads as "the arm is back on the seed" either way."""
    resp = await empty_daemon_client.delete("/move-defaults/pf400")

    assert resp.status_code == 404


async def test_resetting_drops_what_was_tuned(empty_daemon_client) -> None:
    await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"travel_margin": 25.0}},
    )

    assert (await empty_daemon_client.delete("/move-defaults/pf400")).status_code == 204

    body = (await empty_daemon_client.get("/move-defaults/pf400")).json()
    assert set(body["sources"].values()) == {"seed"}


async def test_the_list_is_only_the_tuned_arms_before_a_system_is_mounted(
    empty_daemon_client,
) -> None:
    await empty_daemon_client.patch(
        "/move-defaults/pf400", json={"set": {"travel_margin": 25.0}},
    )

    resp = await empty_daemon_client.get("/move-defaults")

    assert [r["transporter_name"] for r in resp.json()] == ["pf400"]


async def test_a_mounted_system_lists_its_arms_even_untuned(
    mounted_daemon_client,
) -> None:
    resp = await mounted_daemon_client.get("/move-defaults")

    assert [r["transporter_name"] for r in resp.json()] == ["robot1"]
