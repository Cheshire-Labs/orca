"""Setup written through the daemon is in its package folder, so the next daemon has it."""

import json
import logging
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from tests.daemon.fixture_package import write_package
from tests.daemon.started_daemon import started_daemon
from tests.memory_activity import memory_daemon_system
from tests.test_system_runtime import _build_simple_system


async def _post(root: Path, path: str, body: dict[str, object]) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(package_root=root)), base_url="http://daemon.test",
    ) as client:
        (await client.post(path, json=body)).raise_for_status()


async def test_an_access_config_outlives_the_daemon_that_wrote_it(tmp_path: Path) -> None:
    await _post(tmp_path, "/api/access-configs", {"name": "vertical_a", "access_type": "vertical"})

    async with AsyncClient(
        transport=ASGITransport(app=create_app(package_root=tmp_path)), base_url="http://daemon.test",
    ) as next_daemon:
        got = await next_daemon.get("/api/access-configs/vertical_a")

    assert got.status_code == 200
    assert got.json()["access_type"] == "vertical"
    assert "vertical_a" in json.loads((tmp_path / "access_configs.json").read_text(encoding="utf-8"))


async def test_a_mounted_runtime_keeps_its_runs_in_the_package_state_database(tmp_path: Path) -> None:
    write_package(tmp_path)
    app = create_app(package_root=tmp_path)
    async with started_daemon(app) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text

        assert app.state.daemon.holder.runtime.execution_records is app.state.daemon.activity.execution_records
        assert (tmp_path / ".orca" / "state.db").exists()
        await client.post("/api/runtime/unload", json={})



async def test_a_mount_after_an_injected_runtime_uses_the_stores_it_was_given(
    tmp_path: Path,
) -> None:
    write_package(tmp_path)
    system, _ = await _build_simple_system()
    injected = await memory_daemon_system(system)
    await injected.runtime.start()
    app = create_app(package_root=tmp_path, injected=injected)
    async with started_daemon(app) as client:
        (await client.post("/api/runtime/unload", json={})).raise_for_status()
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text

        assert app.state.daemon.holder.runtime.execution_records is injected.activity.execution_records
        await client.post("/api/runtime/unload", json={})
    assert not (tmp_path / ".orca" / "state.db").exists()


async def test_a_broken_setup_file_answers_its_code_and_path(tmp_path: Path) -> None:
    (tmp_path / "access_configs.json").write_text("{not json", encoding="utf-8")

    async with AsyncClient(
        transport=ASGITransport(app=create_app(package_root=tmp_path)), base_url="http://daemon.test",
    ) as client:
        got = await client.get("/api/access-configs")

    assert got.status_code == 500
    detail = got.json()["detail"]
    assert detail["code"] == "setup_file_invalid"
    assert detail["extras"]["path"].endswith("access_configs.json")


async def test_a_broken_setup_file_is_written_to_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "access_configs.json"
    path.write_text("{not json", encoding="utf-8")

    async with AsyncClient(
        transport=ASGITransport(app=create_app(package_root=tmp_path)), base_url="http://daemon.test",
    ) as client:
        await client.get("/api/access-configs")

    refused = [r for r in caplog.records if r.levelno == logging.ERROR and r.name.startswith("orca.")]
    assert any(str(path.resolve()) in r.getMessage() for r in refused)
