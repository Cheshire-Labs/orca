"""CLI dispatch for ``orca move-defaults``.

Validates the verbs parse operator input into the wire body the daemon expects:
only the fields named, plus whichever fields are being handed back to the seed.
"""

from collections.abc import Iterator

import pytest
from cheshire_drivers.move_parameters import SEED_MOVE_PARAMETERS
from typer.testing import CliRunner

from orca.cli import output
from orca.cli.app import app
from orca.daemon.schemas import MoveDefaultsDTO, MoveDefaultsPatchRequest


runner = CliRunner()


def _record(transporter_name: str = "pf400") -> MoveDefaultsDTO:
    return MoveDefaultsDTO(
        transporter_name=transporter_name,
        parameters=SEED_MOVE_PARAMETERS,
        sources=dict.fromkeys(SEED_MOVE_PARAMETERS.model_dump(), "seed"),
    )


class _FakeClient:
    def __init__(self) -> None:
        self.patched: tuple[str, MoveDefaultsPatchRequest] | None = None
        self.reset: str | None = None
        self.shown: str | None = None

    def move_defaults_list(self) -> list[MoveDefaultsDTO]:
        return [_record()]

    def move_defaults_get(self, transporter_name: str) -> MoveDefaultsDTO:
        self.shown = transporter_name
        return _record(transporter_name)

    def move_defaults_patch(
        self, transporter_name: str, body: MoveDefaultsPatchRequest,
    ) -> MoveDefaultsDTO:
        self.patched = (transporter_name, body)
        return _record(transporter_name)

    def move_defaults_reset(self, transporter_name: str) -> None:
        self.reset = transporter_name


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.move_defaults.get_client", lambda: stub)
    yield stub


def test_set_sends_only_the_fields_the_operator_named(fake_client) -> None:
    result = runner.invoke(
        app, ["move-defaults", "set", "pf400", "--travel-margin", "25"],
    )

    assert result.exit_code == 0
    assert fake_client.patched is not None
    name, body = fake_client.patched
    assert name == "pf400"
    assert body.set.model_dump(exclude_none=True) == {"travel_margin": 25.0}
    assert body.clear == []


def test_set_carries_the_fields_being_handed_back_to_the_seed(fake_client) -> None:
    result = runner.invoke(
        app,
        ["move-defaults", "set", "pf400", "--clear", "speed", "--clear", "z_offset"],
    )

    assert result.exit_code == 0
    assert fake_client.patched is not None
    assert fake_client.patched[1].clear == ["speed", "z_offset"]


def test_a_field_named_the_way_its_flag_is_spelled_is_accepted(fake_client) -> None:
    """Every setter is a dashed flag, so an operator types the dashed name to clear
    one; refusing it teaches nothing except that the two halves disagree."""
    result = runner.invoke(
        app, ["move-defaults", "set", "pf400", "--clear", "travel-margin"],
    )

    assert result.exit_code == 0
    assert fake_client.patched is not None
    assert fake_client.patched[1].clear == ["travel_margin"]


def test_set_with_nothing_named_is_a_usage_error(fake_client) -> None:
    """An edit that names no field would report success having changed nothing."""
    result = runner.invoke(app, ["move-defaults", "set", "pf400"])

    assert result.exit_code == output.EXIT_USAGE
    assert fake_client.patched is None


def test_clearing_a_field_the_model_does_not_have_is_a_usage_error(
    fake_client,
) -> None:
    result = runner.invoke(
        app, ["move-defaults", "set", "pf400", "--clear", "grip_height"],
    )

    assert result.exit_code == output.EXIT_USAGE
    assert fake_client.patched is None


def test_the_numbers_a_position_decides_have_no_flag_here(fake_client) -> None:
    """Offering a flag the surface refuses teaches an operator the wrong model of
    where an approach clearance lives."""
    result = runner.invoke(
        app, ["move-defaults", "set", "pf400", "--clearance", "45"],
    )

    assert result.exit_code != 0
    assert fake_client.patched is None


def test_show_reports_where_each_number_came_from(fake_client) -> None:
    result = runner.invoke(app, ["--json", "move-defaults", "show", "pf400"])

    assert result.exit_code == 0
    assert fake_client.shown == "pf400"
    assert '"seed"' in result.stdout


def test_reset_names_the_transporter(fake_client) -> None:
    result = runner.invoke(app, ["move-defaults", "reset", "pf400"])

    assert result.exit_code == 0
    assert fake_client.reset == "pf400"


def test_list_renders_every_arm(fake_client) -> None:
    result = runner.invoke(app, ["--json", "move-defaults", "list"])

    assert result.exit_code == 0
    assert "pf400" in result.stdout
