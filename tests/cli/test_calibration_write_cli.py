"""CLI dispatch tests for ``orca access-configs`` and ``orca deck-layouts``
create/update/delete verbs.

Validates the verb parses input (typed options for access-configs, a
DeckLayoutConfig JSON file for deck-layouts) and calls the control-plane
client with the right value. Wire-shape parity is pinned by
``test_calibration_write_clients``.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from cheshire_drivers.teachpoints import AccessConfig

from orca.cli.app import app
from orca.daemon.schemas import DeckLayoutDTO


runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.ac_added: AccessConfig | None = None
        self.ac_updated: AccessConfig | None = None
        self.ac_deleted: str | None = None
        self.dl_added: tuple[str, str, DeckLayoutConfig] | None = None
        self.dl_updated: tuple[str, str, DeckLayoutConfig] | None = None
        self.dl_deleted: tuple[str, str] | None = None

    def access_configs_add(self, config: AccessConfig) -> AccessConfig:
        self.ac_added = config
        return config

    def access_configs_update(self, config: AccessConfig) -> AccessConfig:
        self.ac_updated = config
        return config

    def access_configs_delete(self, name: str) -> None:
        self.ac_deleted = name

    def deck_layouts_add(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> DeckLayoutDTO:
        self.dl_added = (device_id, name, config)
        return DeckLayoutDTO(device_id=device_id, name=name, config=config)

    def deck_layouts_update(
        self, device_id: str, name: str, config: DeckLayoutConfig,
    ) -> DeckLayoutDTO:
        self.dl_updated = (device_id, name, config)
        return DeckLayoutDTO(device_id=device_id, name=name, config=config)

    def deck_layouts_delete(self, device_id: str, name: str) -> None:
        self.dl_deleted = (device_id, name)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.access_config.get_client", lambda: stub)
    monkeypatch.setattr("orca.cli.deck_layout.get_client", lambda: stub)
    yield stub


@pytest.fixture
def deck_config_file(tmp_path: Path) -> Path:
    path = tmp_path / "deck.json"
    path.write_text(
        json.dumps({"deck_type": "BRAVO_96", "resources": []}),
        encoding="utf-8",
    )
    return path


# -- access-configs ----------------------------------------------------------


def test_access_config_create_invokes_client(fake_client: _FakeClient) -> None:
    result = runner.invoke(
        app,
        [
            "access-configs", "create", "vert_a",
            "--access-type", "vertical",
            "--gripper-offset", "20",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.ac_added is not None
    assert fake_client.ac_added.name == "vert_a"
    assert fake_client.ac_added.access_type == "vertical"
    assert "created access_config" in result.output


def test_access_config_create_rejects_bad_access_type(
    fake_client: _FakeClient,
) -> None:
    result = runner.invoke(
        app,
        ["access-configs", "create", "x", "--access-type", "diagonal"],
    )
    assert result.exit_code != 0
    assert fake_client.ac_added is None


def test_access_config_update_invokes_client(fake_client: _FakeClient) -> None:
    result = runner.invoke(
        app,
        [
            "access-configs", "update", "vert_a",
            "--access-type", "horizontal",
            "--gripper-offset", "33",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.ac_updated is not None
    assert fake_client.ac_updated.access_type == "horizontal"
    assert fake_client.ac_updated.gripper_offset == 33.0


def test_access_config_delete_invokes_client(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["access-configs", "delete", "vert_a"])
    assert result.exit_code == 0, result.output
    assert fake_client.ac_deleted == "vert_a"
    assert "deleted access_config" in result.output


# -- deck-layouts ------------------------------------------------------------


def test_deck_layout_create_invokes_client(
    fake_client: _FakeClient, deck_config_file: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "deck-layouts", "create", "lh1", "primary",
            "--config-file", str(deck_config_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.dl_added is not None
    device_id, name, config = fake_client.dl_added
    assert device_id == "lh1"
    assert name == "primary"
    assert config.deck_type == "BRAVO_96"
    assert "created deck_layout" in result.output


def test_deck_layout_create_rejects_bad_json(
    fake_client: _FakeClient, tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    result = runner.invoke(
        app,
        ["deck-layouts", "create", "lh1", "p", "--config-file", str(bad)],
    )
    assert result.exit_code != 0
    assert fake_client.dl_added is None


def test_deck_layout_update_invokes_client(
    fake_client: _FakeClient, deck_config_file: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "deck-layouts", "update", "lh1", "primary",
            "--config-file", str(deck_config_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.dl_updated is not None
    assert fake_client.dl_updated[0] == "lh1"


def test_deck_layout_delete_invokes_client(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["deck-layouts", "delete", "lh1", "primary"])
    assert result.exit_code == 0, result.output
    assert fake_client.dl_deleted == ("lh1", "primary")
    assert "deleted deck_layout" in result.output


# -- help --------------------------------------------------------------------


@dataclass(frozen=True)
class _HelpExpectation:
    """What a write verb's --help must contain to be considered wired."""

    noun: str
    verb: str
    usage: tuple[str, ...]
    desc: str
    flags: tuple[str, ...]
    # absent_flags belong to sibling verbs; their presence means a wiring mixup.
    absent_flags: tuple[str, ...]


_HELP_EXPECTATIONS: tuple[_HelpExpectation, ...] = (
    _HelpExpectation(
        "access-configs", "create",
        usage=("orca access-configs create", "NAME"),
        desc="Register a new access config",
        flags=("--access-type", "--gripper-offset", "--vertical-clearance",
               "--horizontal-clearance"),
        absent_flags=("--config-file",),
    ),
    _HelpExpectation(
        "access-configs", "update",
        usage=("orca access-configs update", "NAME"),
        desc="Update an access config",
        flags=("--access-type", "--gripper-offset"),
        absent_flags=("--config-file",),
    ),
    _HelpExpectation(
        "access-configs", "delete",
        usage=("orca access-configs delete", "NAME"),
        desc="Delete an access config",
        flags=(),
        absent_flags=("--access-type", "--config-file"),
    ),
    _HelpExpectation(
        "deck-layouts", "create",
        usage=("orca deck-layouts create", "DEVICE_ID", "NAME"),
        desc="Register a new deck layout",
        flags=("--config-file",),
        absent_flags=("--access-type",),
    ),
    _HelpExpectation(
        "deck-layouts", "update",
        usage=("orca deck-layouts update", "DEVICE_ID", "NAME"),
        desc="Update a deck layout",
        flags=("--config-file",),
        absent_flags=("--access-type",),
    ),
    _HelpExpectation(
        "deck-layouts", "delete",
        usage=("orca deck-layouts delete", "DEVICE_ID", "NAME"),
        desc="Delete a deck layout",
        flags=(),
        absent_flags=("--config-file", "--access-type"),
    ),
)


@pytest.mark.parametrize(
    "spec", _HELP_EXPECTATIONS, ids=lambda s: f"{s.noun}-{s.verb}",
)
def test_write_verb_help_renders(spec: _HelpExpectation) -> None:
    """--help must render the verb's real usage, description, and own flags.

    A wide terminal keeps the usage line and option names on single lines so
    the substring assertions are not defeated by Rich's column wrapping.
    """
    result = runner.invoke(
        app, [spec.noun, spec.verb, "--help"], env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    out = result.output
    label = f"{spec.noun} {spec.verb}"
    for token in spec.usage:
        # Case-insensitive: typer 0.27 renders arg metavars as {device_id},
        # older typer as DEVICE_ID; both show the argument in the usage line.
        assert token.lower() in out.lower(), f"usage token {token!r} missing from {label} help"
    assert spec.desc in out, f"description {spec.desc!r} missing from {label} help"
    for flag in spec.flags:
        assert flag in out, f"flag {flag!r} missing from {label} help"
    for flag in spec.absent_flags:
        assert flag not in out, f"flag {flag!r} should not appear in {label} help"
