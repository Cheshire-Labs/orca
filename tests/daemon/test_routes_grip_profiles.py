"""Daemon routes for reading and editing how a labware type is held.

Deployment-scoped like the move defaults: the grip a labware wants is a
property of the labware, so it is editable with no system mounted at all.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A fresh daemon with NO system mounted."""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as c:
        yield c


class TestReading:
    @pytest.mark.asyncio
    async def test_a_type_nobody_measured_reads_as_an_empty_profile(
        self, client: AsyncClient,
    ) -> None:
        resp = await client.get("/grip-profiles/costar_96")

        assert resp.status_code == 200
        assert resp.json() == {"labware_type": "costar_96", "patch": {}}

    @pytest.mark.asyncio
    async def test_the_list_starts_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/grip-profiles")

        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_reading_a_type_does_not_add_it_to_the_list(
        self, client: AsyncClient,
    ) -> None:
        """A GET that seeded a row would make every type anyone ever looked at
        indistinguishable from one somebody measured."""
        await client.get("/grip-profiles/costar_96")

        assert (await client.get("/grip-profiles")).json() == []


class TestEditing:
    @pytest.mark.asyncio
    async def test_a_patch_stores_only_the_fields_named(
        self, client: AsyncClient,
    ) -> None:
        resp = await client.patch(
            "/grip-profiles/costar_96", json={"set": {"resource_width": 76.0}},
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "labware_type": "costar_96", "patch": {"resource_width": 76.0},
        }

    @pytest.mark.asyncio
    async def test_a_second_patch_merges_rather_than_replaces(
        self, client: AsyncClient,
    ) -> None:
        await client.patch(
            "/grip-profiles/costar_96", json={"set": {"resource_width": 76.0}},
        )

        resp = await client.patch(
            "/grip-profiles/costar_96", json={"set": {"z_offset": 2.5}},
        )

        assert resp.json()["patch"] == {"resource_width": 76.0, "z_offset": 2.5}

    @pytest.mark.asyncio
    async def test_clear_hands_a_field_back_in_the_same_request(
        self, client: AsyncClient,
    ) -> None:
        await client.patch(
            "/grip-profiles/costar_96",
            json={"set": {"resource_width": 76.0, "z_offset": 2.5}},
        )

        resp = await client.patch(
            "/grip-profiles/costar_96",
            json={"set": {"resource_width": 80.0}, "clear": ["z_offset"]},
        )

        assert resp.json()["patch"] == {"resource_width": 80.0}

    @pytest.mark.asyncio
    async def test_a_field_name_that_is_not_a_move_parameter_is_refused(
        self, client: AsyncClient,
    ) -> None:
        """Nothing downstream re-checks a cleared field name, so a typo that got
        past the edge would clear nothing and report success."""
        resp = await client.patch(
            "/grip-profiles/costar_96", json={"clear": ["resource_wdith"]},
        )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_an_edited_type_appears_in_the_list(
        self, client: AsyncClient,
    ) -> None:
        await client.patch(
            "/grip-profiles/costar_96", json={"set": {"resource_width": 76.0}},
        )

        listed = (await client.get("/grip-profiles")).json()

        assert [row["labware_type"] for row in listed] == ["costar_96"]


class TestResetting:
    @pytest.mark.asyncio
    async def test_reset_discards_the_profile(self, client: AsyncClient) -> None:
        await client.patch(
            "/grip-profiles/costar_96", json={"set": {"resource_width": 76.0}},
        )

        resp = await client.delete("/grip-profiles/costar_96")

        assert resp.status_code == 204
        assert (await client.get("/grip-profiles")).json() == []

    @pytest.mark.asyncio
    async def test_resetting_a_type_with_no_profile_is_a_404(
        self, client: AsyncClient,
    ) -> None:
        resp = await client.delete("/grip-profiles/never_measured")

        assert resp.status_code == 404
