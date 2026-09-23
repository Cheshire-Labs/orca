"""CLI tests for ``orca labware catalog``.

Validates dispatch + rendering. Wire-shape parity with a hosted deployment is pinned
by the cloud backend's typed-response tests.
"""

from collections.abc import Iterator, Sequence
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.control_plane import LabwareCatalogSummaryDTO


runner = CliRunner()


class _FakeCloudClient:
    """Stand-in for the cloud backend client's ``list_labware``."""

    def __init__(self, rows: Sequence[LabwareCatalogSummaryDTO]) -> None:
        self._rows = list(rows)
        self.captured_category: str | None | object = object()

    def list_labware(
        self, category: str | None = None,
    ) -> Sequence[LabwareCatalogSummaryDTO]:
        self.captured_category = category
        if category is None:
            return list(self._rows)
        return [r for r in self._rows if r.category == category]


@pytest.fixture
def fake_cloud_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_FakeCloudClient]:
    rows = [
        LabwareCatalogSummaryDTO(
            labware_type="Cor_96_demo", display_name="Corning demo", category="plate",
            vendor="Corning", source="plr_seed", plr_class_name="Cor_96_demo",
        ),
        LabwareCatalogSummaryDTO(
            labware_type="HTF_demo", display_name="HTF tips", category="tip_rack",
            vendor="Hamilton", source="plr_seed", plr_class_name="HTF_demo",
        ),
        LabwareCatalogSummaryDTO(
            labware_type="PLT_CAR_demo", display_name="Plate carrier", category="carrier",
            vendor="Hamilton", source="plr_seed", plr_class_name="PLT_CAR_demo",
        ),
    ]
    stub = _FakeCloudClient(rows)
    monkeypatch.setattr("orca.cli.labware.get_client", lambda: stub)
    yield stub


def test_labware_catalog_lists_every_row(
    fake_cloud_client: _FakeCloudClient,
) -> None:
    # --json is the top-level app flag, so it comes BEFORE the noun verb.
    result = runner.invoke(app, ["--json", "labware", "catalog"])
    assert result.exit_code == 0, result.output
    # JSON mode emits the typed entries verbatim.
    assert '"Cor_96_demo"' in result.output
    assert '"HTF_demo"' in result.output
    assert '"PLT_CAR_demo"' in result.output
    # No --category passed -> client called with None.
    assert fake_cloud_client.captured_category is None


def test_labware_catalog_filters_by_category(
    fake_cloud_client: _FakeCloudClient,
) -> None:
    result = runner.invoke(
        app, ["--json", "labware", "catalog", "--category", "plate"],
    )
    assert result.exit_code == 0, result.output
    assert '"Cor_96_demo"' in result.output
    assert '"HTF_demo"' not in result.output
    assert '"PLT_CAR_demo"' not in result.output
    assert fake_cloud_client.captured_category == "plate"


def test_labware_catalog_table_mode_renders_columns(
    fake_cloud_client: _FakeCloudClient,
) -> None:
    """Default (table) output carries labware_type + category + display_name."""
    result = runner.invoke(app, ["labware", "catalog"])
    assert result.exit_code == 0, result.output
    assert "Cor_96_demo" in result.output
    assert "plate" in result.output
    assert "Corning demo" in result.output


def test_labware_catalog_help_renders() -> None:
    result = runner.invoke(app, ["labware", "catalog", "--help"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "catalog" in out
    assert "category" in out
