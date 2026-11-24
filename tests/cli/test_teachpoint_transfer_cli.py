"""`orca teachpoints export` writes a file that `orca teachpoints import` takes as it is."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue
from typer.testing import CliRunner

from orca.cli import output
from orca.cli.app import app
from orca.operations.teachpoint_transfer_models import CreateTeachpointsResponse
from orca.operations.wire_models import TeachpointDTO

runner = CliRunner()

_ROW = TeachpointDTO(
    device_id="robot1", position_id="slot_x", coord_type="cartesian",
    coords={"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0},
    access_config_name=None, gateway=None, orientation="right", taught_with="costar_96",
)


class _FakeClient:
    def __init__(self) -> None:
        self.imported: tuple[str, list[dict[str, JsonValue]], bool, str | None] | None = None

    def teachpoints_list(self, device_id: str) -> list[TeachpointDTO]:
        assert device_id == "robot1"
        return [_ROW]

    def teachpoints_create(
        self, device_id: str, teachpoints: Sequence[dict[str, JsonValue]],
        overwrite: bool = False, *, reason: str | None = None,
    ) -> CreateTeachpointsResponse:
        self.imported = (device_id, list(teachpoints), overwrite, reason)
        return CreateTeachpointsResponse(
            total=1, created=1, updated=0, summary="saved 1 teachpoints (1 created, 0 updated)",
        )


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.teachpoint.get_client", lambda: stub)
    return stub


def test_export_writes_the_rows_the_server_returned(fake_client: _FakeClient, tmp_path: Path) -> None:
    path = tmp_path / "robot1.json"

    result = runner.invoke(app, ["teachpoints", "export", "robot1", "--out", str(path)])

    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text(encoding="utf-8")) == [_ROW.model_dump(mode="json")]


def test_export_prints_the_rows_in_json_mode(fake_client: _FakeClient) -> None:
    try:
        result = runner.invoke(app, ["--json", "teachpoints", "export", "robot1"])
    finally:
        output.set_mode(output.OutputMode.TABLE)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [_ROW.model_dump(mode="json")]


def test_import_sends_an_exported_file_as_it_is(fake_client: _FakeClient, tmp_path: Path) -> None:
    path = tmp_path / "robot1.json"
    runner.invoke(app, ["teachpoints", "export", "robot1", "--out", str(path)])

    result = runner.invoke(app, [
        "teachpoints", "import", "robot2", str(path), "--overwrite", "--reason", "restore",
    ])

    assert result.exit_code == 0, result.output
    assert fake_client.imported == ("robot2", [_ROW.model_dump(mode="json")], True, "restore")
    assert "saved 1 teachpoints" in result.output


@pytest.mark.parametrize("content", ['{"position_id": "a"}', "[1, 2]", "not json"])
def test_import_refuses_a_file_that_is_not_a_list_of_objects(
    fake_client: _FakeClient, tmp_path: Path, content: str,
) -> None:
    path = tmp_path / "rows.json"
    path.write_text(content, encoding="utf-8")

    result = runner.invoke(app, ["teachpoints", "import", "robot1", str(path)])

    assert result.exit_code == output.EXIT_USAGE, result.output
    assert fake_client.imported is None
