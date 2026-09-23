"""CLI dispatch tests for ``orca teachpoints create/update/delete``.

Validates the verb parses input (typed scalar options + a coords JSON
string or file) and calls the control-plane client with the right values.
Wire-shape parity is pinned by ``test_teachpoint_write_clients``.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.daemon.schemas import TeachpointDTO


runner = CliRunner()


_CARTESIAN = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0}


def _dto(position_id: str = "slot_x") -> TeachpointDTO:
    return TeachpointDTO(
        device_id="robot1", position_id=position_id, coord_type="cartesian",
        coords={"type": "cartesian", **_CARTESIAN},
        access_config_name="vert_a", gateway=None, orientation="right",
    )


class _FakeClient:
    def __init__(self) -> None:
        self.added: dict | None = None
        self.updated: dict | None = None
        self.deleted: tuple[str, str] | None = None

    def teachpoints_add(
        self, device_id: str, position_id: str, coord_type: str,
        coords: dict, access_config_name: str | None,
        gateway: str | None, orientation: str | None,
        taught_with: str | None = None,
    ) -> TeachpointDTO:
        self.added = {
            "taught_with": taught_with,
            "device_id": device_id, "position_id": position_id,
            "coord_type": coord_type, "coords": coords,
            "access_config_name": access_config_name,
            "gateway": gateway, "orientation": orientation,
        }
        return _dto(position_id)

    def teachpoints_update(
        self, device_id: str, position_id: str, coords: dict,
        access_config_name: str | None, gateway: str | None,
        orientation: str | None, update_access_config: bool,
        update_gateway: bool, update_orientation: bool,
        taught_with: str | None = None, update_taught_with: bool = False,
    ) -> TeachpointDTO:
        self.updated = {
            "taught_with": taught_with,
            "update_taught_with": update_taught_with,
            "device_id": device_id, "position_id": position_id,
            "coords": coords, "access_config_name": access_config_name,
            "gateway": gateway, "orientation": orientation,
            "update_access_config": update_access_config,
            "update_gateway": update_gateway,
            "update_orientation": update_orientation,
        }
        return _dto(position_id)

    def teachpoints_delete(self, device_id: str, position_id: str) -> None:
        self.deleted = (device_id, position_id)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.teachpoint.get_client", lambda: stub)
    yield stub


def test_create_with_inline_coords(fake_client: _FakeClient) -> None:
    result = runner.invoke(
        app,
        [
            "teachpoints", "create", "robot1", "slot_x",
            "--coord-type", "cartesian",
            "--coords", json.dumps(_CARTESIAN),
            "--access-config", "vert_a",
            "--orientation", "right",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.added is not None
    assert fake_client.added["device_id"] == "robot1"
    assert fake_client.added["position_id"] == "slot_x"
    assert fake_client.added["coords"]["x"] == 1.0
    assert fake_client.added["access_config_name"] == "vert_a"
    assert fake_client.added["orientation"] == "right"
    assert "created teachpoint" in result.output


def test_create_with_coords_file(
    fake_client: _FakeClient, tmp_path: Path,
) -> None:
    path = tmp_path / "coords.json"
    path.write_text(json.dumps(_CARTESIAN), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "teachpoints", "create", "robot1", "slot_y",
            "--coord-type", "cartesian",
            "--coords-file", str(path),
            "--access-config", "vert_a",
            "--orientation", "left",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.added is not None
    assert fake_client.added["coords"]["x"] == 1.0


def test_create_rejects_both_coords_sources(
    fake_client: _FakeClient, tmp_path: Path,
) -> None:
    path = tmp_path / "coords.json"
    path.write_text(json.dumps(_CARTESIAN), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "teachpoints", "create", "robot1", "z",
            "--coord-type", "cartesian",
            "--coords", json.dumps(_CARTESIAN),
            "--coords-file", str(path),
        ],
    )
    assert result.exit_code != 0
    assert fake_client.added is None


def test_create_rejects_missing_coords(fake_client: _FakeClient) -> None:
    result = runner.invoke(
        app,
        ["teachpoints", "create", "robot1", "z", "--coord-type", "joint"],
    )
    assert result.exit_code != 0
    assert fake_client.added is None


def test_create_rejects_bad_json(fake_client: _FakeClient) -> None:
    result = runner.invoke(
        app,
        [
            "teachpoints", "create", "robot1", "z",
            "--coord-type", "cartesian", "--coords", "{not json",
        ],
    )
    assert result.exit_code != 0
    assert fake_client.added is None


def test_create_accepts_integer_coords(fake_client: _FakeClient) -> None:
    """json.loads yields int for unquoted whole numbers; the loader must
    accept them (the wire type is str | int | float)."""
    int_coords = {"rail": 5, "base": 10}
    result = runner.invoke(
        app,
        [
            "teachpoints", "create", "robot1", "slot_int",
            "--coord-type", "joint",
            "--coords", json.dumps(int_coords),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.added is not None
    assert fake_client.added["coords"] == {"rail": 5, "base": 10}


def test_create_rejects_non_scalar_coord_value(fake_client: _FakeClient) -> None:
    """Nested / list / null coord values are not wire-legal and must fail."""
    result = runner.invoke(
        app,
        [
            "teachpoints", "create", "robot1", "z",
            "--coord-type", "cartesian",
            "--coords", json.dumps({"x": [1, 2, 3]}),
        ],
    )
    assert result.exit_code != 0
    assert fake_client.added is None


def test_create_rejects_bool_coord_value(fake_client: _FakeClient) -> None:
    """bool is a JSON scalar but not a coordinate; reject it explicitly."""
    result = runner.invoke(
        app,
        [
            "teachpoints", "create", "robot1", "z",
            "--coord-type", "cartesian",
            "--coords", json.dumps({"x": True}),
        ],
    )
    assert result.exit_code != 0
    assert fake_client.added is None


def test_update_sets_flags_for_passed_options(fake_client: _FakeClient) -> None:
    result = runner.invoke(
        app,
        [
            "teachpoints", "update", "robot1", "slot_x",
            "--coords", json.dumps(_CARTESIAN),
            "--access-config", "horiz_b",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.updated is not None
    assert fake_client.updated["update_access_config"] is True
    assert fake_client.updated["access_config_name"] == "horiz_b"
    # gateway / orientation not passed -> their update flags stay False
    assert fake_client.updated["update_gateway"] is False
    assert fake_client.updated["update_orientation"] is False


def test_update_no_flags_when_only_coords(fake_client: _FakeClient) -> None:
    result = runner.invoke(
        app,
        [
            "teachpoints", "update", "robot1", "slot_x",
            "--coords", json.dumps(_CARTESIAN),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.updated is not None
    assert fake_client.updated["update_access_config"] is False
    assert fake_client.updated["update_gateway"] is False
    assert fake_client.updated["update_orientation"] is False


def test_delete_invokes_client(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["teachpoints", "delete", "robot1", "slot_x"])
    assert result.exit_code == 0, result.output
    assert fake_client.deleted == ("robot1", "slot_x")
    assert "deleted teachpoint" in result.output


@dataclass(frozen=True)
class _HelpExpectation:
    """What a teachpoint write verb's --help must contain to be wired."""

    verb: str
    usage: tuple[str, ...]
    flags: tuple[str, ...]
    absent_flags: tuple[str, ...]


_HELP_EXPECTATIONS: tuple[_HelpExpectation, ...] = (
    _HelpExpectation(
        "create",
        usage=("orca teachpoints create", "DEVICE_ID", "POSITION_ID"),
        flags=("--coord-type", "--coords", "--coords-file",
               "--access-config", "--gateway", "--orientation"),
        absent_flags=(),
    ),
    _HelpExpectation(
        "update",
        usage=("orca teachpoints update", "DEVICE_ID", "POSITION_ID"),
        flags=("--coords", "--coords-file", "--access-config",
               "--gateway", "--orientation"),
        # coord_type is preserved on update; create's required flag must not leak here.
        absent_flags=("--coord-type",),
    ),
    _HelpExpectation(
        "delete",
        usage=("orca teachpoints delete", "DEVICE_ID", "POSITION_ID"),
        flags=(),
        absent_flags=("--coords", "--coord-type", "--access-config"),
    ),
)


@pytest.mark.parametrize(
    "spec", _HELP_EXPECTATIONS, ids=lambda s: s.verb,
)
def test_write_verb_help_renders(spec: _HelpExpectation) -> None:
    """--help renders the verb's real usage, positional args, and own flags.

    COLUMNS=200 keeps the usage line and option names unwrapped so substring
    assertions are not defeated by Rich's column wrapping.
    """
    result = runner.invoke(
        app, ["teachpoints", spec.verb, "--help"], env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    out = result.output
    for token in spec.usage:
        # Case-insensitive: typer 0.27 renders arg metavars as {device_id},
        # older typer as DEVICE_ID; both show the argument in the usage line.
        assert token.lower() in out.lower(), f"usage token {token!r} missing from {spec.verb} help"
    for flag in spec.flags:
        assert flag in out, f"flag {flag!r} missing from {spec.verb} help"
    for flag in spec.absent_flags:
        assert flag not in out, f"flag {flag!r} should not appear in {spec.verb} help"
