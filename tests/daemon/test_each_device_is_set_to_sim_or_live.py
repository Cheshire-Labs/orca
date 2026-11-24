"""Each device is set to sim or live through one Operation, kept in the database."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import JsonValue

from orca.daemon.app import create_app
from tests.daemon.fixture_package import write_package
from tests.daemon.started_daemon import started_daemon

_REAL_ARM: dict[str, JsonValue] = {"client": "lab_pc", "real": {"type": "human"}}


@asynccontextmanager
async def _daemon(root: Path) -> AsyncIterator[AsyncClient]:
    async with started_daemon(create_app(package_root=root)) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        try:
            yield client
        finally:
            await client.post("/api/runtime/unload")


def _package(tmp_path: Path, setups: dict[str, dict[str, JsonValue]]) -> Path:
    root = write_package(tmp_path / "package")
    (root / "devices").mkdir()
    for name, setup in setups.items():
        (root / "devices" / f"{name}.json").write_text(json.dumps(setup), encoding="utf-8")
    return root


async def _setting(client: AsyncClient, device_name: str) -> str:
    entry = await client.get(f"/api/devices/{device_name}")
    assert entry.status_code == 200, entry.text
    setting: str = entry.json()["engine"]["setting"]
    return setting


@pytest.mark.asyncio
async def test_a_device_with_no_setting_reads_sim(tmp_path: Path) -> None:
    async with _daemon(_package(tmp_path, {})) as client:
        assert await _setting(client, "shaker1") == "SIM"


@pytest.mark.asyncio
async def test_a_device_with_a_real_driver_is_set_to_live_and_keeps_it_after_a_restart(tmp_path: Path) -> None:
    root = _package(tmp_path, {"robot1": _REAL_ARM})
    async with _daemon(root) as client:
        changed = await client.put(
            "/api/devices/robot1/mode", json={"mode": "LIVE", "confirm": True},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["mode"] == "LIVE"

    async with _daemon(root) as client:
        assert await _setting(client, "robot1") == "LIVE"


@pytest.mark.asyncio
async def test_a_device_with_no_real_driver_cannot_be_set_to_live(tmp_path: Path) -> None:
    async with _daemon(_package(tmp_path, {})) as client:
        refused = await client.put(
            "/api/devices/shaker1/mode", json={"mode": "LIVE"},
        )

        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "device_has_no_real_driver"
        assert await _setting(client, "shaker1") == "SIM"


@pytest.mark.asyncio
async def test_an_always_sim_device_cannot_be_set_to_live(tmp_path: Path) -> None:
    async with _daemon(_package(tmp_path, {"robot1": {**_REAL_ARM, "always_sim": True}})) as client:
        refused = await client.put(
            "/api/devices/robot1/mode", json={"mode": "LIVE"},
        )

        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "device_has_no_real_driver"


@pytest.mark.asyncio
async def test_an_unknown_device_is_not_found(tmp_path: Path) -> None:
    async with _daemon(_package(tmp_path, {})) as client:
        missing = await client.put(
            "/api/devices/nope/mode", json={"mode": "SIM"},
        )

        assert missing.status_code == 404, missing.text


_REAL_SHAKER: dict[str, JsonValue] = {"client": "lab_pc", "real": {"type": "plr", "backend": "ShakerChatterboxBackend"}}


async def _volumes_provenance(client: AsyncClient, labware_id: str) -> str:
    read = await client.get(f"/api/labware-instances/{labware_id}/contents")
    assert read.status_code == 200, read.text
    provenance: str = read.json()["provenance"]
    return provenance


@pytest.mark.asyncio
async def test_a_switch_answers_the_warning_first_and_switches_only_when_confirmed(tmp_path: Path) -> None:
    async with _daemon(_package(tmp_path, {"shaker1": _REAL_SHAKER})) as client:
        registered = await client.post(
            "/api/labware-instances",
            json={"template_name": "plate_96", "location": "shaker1", "reason": "test"},
        )
        assert registered.status_code == 200, registered.text
        labware_id: str = registered.json()["labware"]["id"]
        volumes = await client.put(
            f"/api/labware-instances/{labware_id}/well-volumes",
            json={"well_volumes": {"A1": 50.0}, "reason": "test"},
        )
        assert volumes.status_code == 200, volumes.text
        assert await _volumes_provenance(client, labware_id) == "known"

        asked = await client.put(
            "/api/devices/shaker1/mode", json={"mode": "LIVE"},
        )
        assert asked.status_code == 200, asked.text
        assert asked.json()["switched"] is False
        assert "carries over" in asked.json()["warning"]
        assert await _setting(client, "shaker1") == "SIM"
        assert await _volumes_provenance(client, labware_id) == "known"

        switched = await client.put(
            "/api/devices/shaker1/mode", json={"mode": "LIVE", "confirm": True},
        )

        assert switched.status_code == 200, switched.text
        assert switched.json()["switched"] is True
        assert switched.json()["warning"] == asked.json()["warning"]
        assert await _volumes_provenance(client, labware_id) == "stale"


@pytest.mark.asyncio
async def test_setting_the_mode_a_device_already_has_warns_of_nothing(tmp_path: Path) -> None:
    async with _daemon(_package(tmp_path, {})) as client:
        unchanged = await client.put(
            "/api/devices/shaker1/mode", json={"mode": "SIM"},
        )

        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["warning"] is None
