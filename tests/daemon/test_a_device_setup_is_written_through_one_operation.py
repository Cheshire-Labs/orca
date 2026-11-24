"""`devices/<name>.json` has one REST Operation that writes it, with or without a loaded system."""

import json
from pathlib import Path

import pytest

from orca.daemon.app import create_app
from tests.daemon.fixture_package import write_package
from tests.daemon.started_daemon import started_daemon

_REAL_ARM = {"client": "lab_pc", "real": {"type": "human"}}


@pytest.mark.asyncio
async def test_a_setup_is_written_to_its_file_and_read_back(tmp_path: Path) -> None:
    root = write_package(tmp_path / "package")
    async with started_daemon(create_app(package_root=root)) as client:
        written = await client.put(
            "/api/devices/robot1/setup", json={"setup": _REAL_ARM, "reason": "bought it"},
        )
        assert written.status_code == 200, written.text
        assert "next reload" in written.json()["reload_note"]

        read = await client.get("/api/devices/robot1/setup")

    assert read.status_code == 200, read.text
    assert read.json()["setup"]["real"]["type"] == "human"
    assert json.loads((root / "devices" / "robot1.json").read_text(encoding="utf-8")) == _REAL_ARM


@pytest.mark.asyncio
async def test_a_device_with_no_file_reads_as_the_default_sim(tmp_path: Path) -> None:
    root = write_package(tmp_path / "package")
    async with started_daemon(create_app(package_root=root)) as client:
        read = await client.get("/api/devices/shaker1/setup")

    assert read.status_code == 200, read.text
    assert read.json()["setup"]["real"] is None
    assert not (root / "devices" / "shaker1.json").exists()


@pytest.mark.asyncio
async def test_with_a_system_loaded_a_setup_for_no_device_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    root = write_package(tmp_path / "package")
    async with started_daemon(create_app(package_root=root)) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        try:
            refused = await client.put(
                "/api/devices/nope/setup", json={"setup": _REAL_ARM},
            )
        finally:
            await client.post("/api/runtime/unload")

    assert refused.status_code == 422, refused.text
    assert "names no device" in refused.json()["detail"]["message"]
    assert not (root / "devices" / "nope.json").exists()


@pytest.mark.asyncio
async def test_a_setup_the_model_refuses_is_never_written(tmp_path: Path) -> None:
    root = write_package(tmp_path / "package")
    async with started_daemon(create_app(package_root=root)) as client:
        refused = await client.put(
            "/api/devices/robot1/setup",
            json={"setup": {"sim": {"use_vendor_sim": True}}},
        )

    assert refused.status_code == 422, refused.text
    assert not (root / "devices" / "robot1.json").exists()


@pytest.mark.asyncio
async def test_a_setup_with_a_simulated_fault_is_written_so_it_reads_back(tmp_path: Path) -> None:
    """A fault's `kind` says which fault it is. It is the model's default, and the file must still carry it."""
    root = write_package(tmp_path / "package")
    setup = {"sim": {"faults": [{"kind": "raise", "method": "shake"}]}}
    async with started_daemon(create_app(package_root=root)) as client:
        written = await client.put("/api/devices/robot1/setup", json={"setup": setup, "reason": "a fault to test"})
        assert written.status_code == 200, written.text

        read = await client.get("/api/devices/robot1/setup")

    assert read.status_code == 200, read.text
    assert read.json()["setup"]["sim"]["faults"][0]["kind"] == "raise"
    on_disk = json.loads((root / "devices" / "robot1.json").read_text(encoding="utf-8"))
    assert on_disk == {"sim": {"faults": [{"kind": "raise", "method": "shake"}]}}
