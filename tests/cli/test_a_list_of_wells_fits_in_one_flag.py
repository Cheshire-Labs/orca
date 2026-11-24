"""A remedy fills `--position <positions>` or `--well <well_volumes>` with a list; the CLI takes it comma-separated."""

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.operations.labware_models import MarkTipsUsedResponse


class _Labware:
    id = "rack-1"


class _FakeClient:
    def __init__(self) -> None:
        self.volumes: dict[str, float] = {}
        self.used: list[str] = []
        self.present: list[str] | None = None

    def labware_list(self) -> list[_Labware]:
        return [_Labware()]

    def labware_set_well_volumes(self, labware_id: str, well_volumes: dict[str, float], reason: str) -> None:
        self.volumes = well_volumes

    def labware_mark_tips_used(self, labware_id: str, positions: list[str], reason: str) -> MarkTipsUsedResponse:
        self.used = positions
        return MarkTipsUsedResponse(labware_id=labware_id, tip_positions_present=[])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.labware.get_client", lambda: stub)
    yield stub


def test_set_volume_takes_several_wells_in_one_flag(client: _FakeClient) -> None:
    result = CliRunner().invoke(
        app, ["labware", "set-volume", "rack-1", "--well", "A1=50, B1=40", "--well", "C1=5", "--reason", "r"])

    assert result.exit_code == 0, result.output
    assert client.volumes == {"A1": 50.0, "B1": 40.0, "C1": 5.0}


def test_mark_tips_used_takes_several_positions_in_one_flag(client: _FakeClient) -> None:
    result = CliRunner().invoke(
        app, ["labware", "mark-tips-used", "rack-1", "--position", "A1,B1", "-p", "C1", "--reason", "r"])

    assert result.exit_code == 0, result.output
    assert client.used == ["A1", "B1", "C1"]
