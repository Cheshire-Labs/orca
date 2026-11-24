"""On the framework, a device whose setup names no orca-client goes to the user's: `ORCA_DEFAULT_CLIENT`, else `local`."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from orca.daemon.app import create_app
from orca.devices.shaker import Shaker
from orca.resource_models.devices import Device
from orca.runtime.device_assignment import assign_devices
from orca.sdk.devices import Sealer


def _client_of(
    name: str, package_root: Path, monkeypatch: pytest.MonkeyPatch, default_client: str | None, device: Device | None = None,
) -> str | None:
    if default_client is None:
        monkeypatch.delenv("ORCA_DEFAULT_CLIENT", raising=False)
    else:
        monkeypatch.setenv("ORCA_DEFAULT_CLIENT", default_client)
    store = create_app(package_root=package_root).state.daemon.setup.device_setups()
    (assigned,) = assign_devices([device or Shaker(name=name)], store)
    return assigned.setup.client


def _write_setup(package_root: Path, name: str, setup: dict[str, JsonValue]) -> None:
    (package_root / "devices").mkdir()
    (package_root / "devices" / f"{name}.json").write_text(json.dumps(setup), encoding="utf-8")


def test_a_device_with_no_setup_file_goes_to_the_local_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client_of("shaker_1", tmp_path, monkeypatch, None) == "local"


def test_a_setup_file_that_names_no_client_goes_to_the_local_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_setup(tmp_path, "shaker_1", {"always_sim": True})
    assert _client_of("shaker_1", tmp_path, monkeypatch, None) == "local"


def test_a_real_device_whose_setup_names_no_client_goes_to_the_local_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real: JsonValue = {"type": "plr", "backend": "A4SBackend", "connection": {"type": "serial", "port": "COM3"}}
    _write_setup(tmp_path, "sealer_1", {"real": real})
    assert _client_of("sealer_1", tmp_path, monkeypatch, None, Sealer("sealer_1")) == "local"


def test_orca_default_client_names_another_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client_of("shaker_1", tmp_path, monkeypatch, "bench_pc") == "bench_pc"


def test_a_setup_file_that_names_its_client_keeps_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_setup(tmp_path, "shaker_1", {"client": "lab_pc_2"})
    assert _client_of("shaker_1", tmp_path, monkeypatch, None) == "lab_pc_2"
