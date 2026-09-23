"""CLI tests for ``orca labware get/add/update/delete``.

Validates dispatch + rendering. Wire-shape parity with a hosted deployment is pinned
by the cloud backend's typed-response tests.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.control_plane import LabwareCatalogEntryDTO


runner = CliRunner()


_SAMPLE_GEOMETRY: dict[str, Any] = {
    "category": "plate",
    "labware_type": "Cust_demo",
    "display_name": "Custom demo plate",
    "num_rows": 1,
    "num_cols": 1,
    "size_x": 127.0,
    "size_y": 85.0,
    "size_z": 14.0,
    "wells": [],
}


def _entry(**overrides: Any) -> LabwareCatalogEntryDTO:
    base: dict[str, Any] = {
        "labware_type": "Cust_demo",
        "display_name": "Custom demo plate",
        "category": "plate",
        "vendor": "Operator Labs",
        "source": "operator_custom",
        "geometry": _SAMPLE_GEOMETRY,
        "plr_class_name": None,
    }
    base.update(overrides)
    return LabwareCatalogEntryDTO(**base)


class _FakeCloudClient:
    """Records arguments for each CRUD method and returns a canned entry."""

    def __init__(self, get_entry: LabwareCatalogEntryDTO | None = None) -> None:
        self.get_called_with: str | None = None
        self.get_entry = get_entry or _entry()
        self.add_called_with: dict[str, Any] | None = None
        self.update_called_with: tuple[str, dict[str, Any]] | None = None
        self.delete_called_with: str | None = None

    def get_labware(self, labware_type: str) -> LabwareCatalogEntryDTO:
        self.get_called_with = labware_type
        return self.get_entry

    def add_labware(
        self,
        *,
        labware_type: str,
        display_name: str,
        category: str,
        geometry: dict[str, Any],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntryDTO:
        self.add_called_with = {
            "labware_type": labware_type,
            "display_name": display_name,
            "category": category,
            "geometry": geometry,
            "vendor": vendor,
            "plr_class_name": plr_class_name,
        }
        return _entry(
            labware_type=labware_type,
            display_name=display_name,
            category=category,
            vendor=vendor,
            plr_class_name=plr_class_name,
            geometry=geometry,
        )

    def update_labware(
        self,
        labware_type: str,
        *,
        display_name: str,
        category: str,
        geometry: dict[str, Any],
        vendor: str | None = None,
        plr_class_name: str | None = None,
    ) -> LabwareCatalogEntryDTO:
        self.update_called_with = (
            labware_type,
            {
                "display_name": display_name,
                "category": category,
                "geometry": geometry,
                "vendor": vendor,
                "plr_class_name": plr_class_name,
            },
        )
        return _entry(
            labware_type=labware_type,
            display_name=display_name,
            category=category,
            vendor=vendor,
            plr_class_name=plr_class_name,
            geometry=geometry,
        )

    def delete_labware(self, labware_type: str) -> None:
        self.delete_called_with = labware_type


@pytest.fixture
def fake_cloud_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeCloudClient]:
    stub = _FakeCloudClient()
    monkeypatch.setattr("orca.cli.labware.get_client", lambda: stub)
    yield stub


@pytest.fixture
def geometry_file(tmp_path: Path) -> Path:
    path = tmp_path / "geometry.json"
    path.write_text(json.dumps(_SAMPLE_GEOMETRY), encoding="utf-8")
    return path


# ---- orca labware get ----


def test_labware_get_fetches_by_type(fake_cloud_client: _FakeCloudClient) -> None:
    result = runner.invoke(app, ["--json", "labware", "get", "Cust_demo"])
    assert result.exit_code == 0, result.output
    assert fake_cloud_client.get_called_with == "Cust_demo"
    assert '"labware_type": "Cust_demo"' in result.output
    assert '"source": "operator_custom"' in result.output


def test_labware_get_table_mode_renders_kv(
    fake_cloud_client: _FakeCloudClient,
) -> None:
    result = runner.invoke(app, ["labware", "get", "Cust_demo"])
    assert result.exit_code == 0, result.output
    assert "Cust_demo" in result.output
    assert "plate" in result.output
    assert "operator_custom" in result.output


# ---- orca labware add ----


def test_labware_add_sends_create_request(
    fake_cloud_client: _FakeCloudClient, geometry_file: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "--json", "labware", "add", "Cust_demo",
            "--display-name", "Custom demo plate",
            "--category", "plate",
            "--vendor", "Operator Labs",
            "--geometry-file", str(geometry_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_cloud_client.add_called_with is not None
    args = fake_cloud_client.add_called_with
    assert args["labware_type"] == "Cust_demo"
    assert args["category"] == "plate"
    assert args["vendor"] == "Operator Labs"
    assert args["plr_class_name"] is None
    assert args["geometry"]["size_x"] == 127.0


def test_labware_add_table_mode_confirms_creation(
    fake_cloud_client: _FakeCloudClient, geometry_file: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "labware", "add", "Cust_demo",
            "--display-name", "Custom demo plate",
            "--category", "plate",
            "--geometry-file", str(geometry_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "added labware" in result.output
    assert "Cust_demo" in result.output


# ---- orca labware update ----


def test_labware_update_sends_put_request(
    fake_cloud_client: _FakeCloudClient, geometry_file: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "--json", "labware", "update", "Cust_demo",
            "--display-name", "renamed demo",
            "--category", "plate",
            "--geometry-file", str(geometry_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_cloud_client.update_called_with is not None
    labware_type, args = fake_cloud_client.update_called_with
    assert labware_type == "Cust_demo"
    assert args["display_name"] == "renamed demo"


# ---- orca labware delete ----


def test_labware_delete_calls_delete(fake_cloud_client: _FakeCloudClient) -> None:
    result = runner.invoke(app, ["labware", "delete", "Cust_demo"])
    assert result.exit_code == 0, result.output
    assert fake_cloud_client.delete_called_with == "Cust_demo"
    assert "deleted labware" in result.output


# ---- help ----


@pytest.mark.parametrize("verb", ["get", "add", "update", "delete"])
def test_labware_crud_help_renders(verb: str) -> None:
    result = runner.invoke(app, ["labware", verb, "--help"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "labware" in out
