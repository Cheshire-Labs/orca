"""CLI dispatch for ``orca grip-profiles``.

Covers the rendered (non-JSON) path too: an operator reading a list in a
terminal is the common case, and a table that raises on render is a surface
that does not exist.
"""

from collections.abc import Iterator

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from typer.testing import CliRunner

from orca.cli import output
from orca.cli.app import app
from orca.daemon.schemas import GripProfileDTO, GripProfilePatchRequest


runner = CliRunner()


def _record(
    labware_type: str = "costar_96",
    patch: MoveParameterPatch | None = None,
) -> GripProfileDTO:
    return GripProfileDTO(
        labware_type=labware_type,
        patch=patch if patch is not None else MoveParameterPatch(resource_width=76.0),
    )


class _FakeClient:
    def __init__(self) -> None:
        self.patched: tuple[str, GripProfilePatchRequest] | None = None
        self.reset: str | None = None
        self.shown: str | None = None

    def grip_profiles_list(self) -> list[GripProfileDTO]:
        return [_record()]

    def grip_profiles_get(self, labware_type: str) -> GripProfileDTO:
        self.shown = labware_type
        return _record(labware_type)

    def grip_profiles_patch(
        self, labware_type: str, body: GripProfilePatchRequest,
    ) -> GripProfileDTO:
        self.patched = (labware_type, body)
        return _record(labware_type)

    def grip_profiles_reset(self, labware_type: str) -> None:
        self.reset = labware_type


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.grip_profiles.get_client", lambda: stub)
    yield stub


def test_list_renders_a_table_without_json_mode(fake_client) -> None:
    """The terminal path, not the machine path: this is what an operator runs."""
    result = runner.invoke(app, ["grip-profiles", "list"])

    assert result.exit_code == 0, result.output
    assert "costar_96" in result.stdout


def test_show_renders_the_measured_fields(fake_client) -> None:
    result = runner.invoke(app, ["grip-profiles", "show", "costar_96"])

    assert result.exit_code == 0, result.output
    assert fake_client.shown == "costar_96"
    assert "resource_width" in result.stdout


def test_show_says_so_when_a_type_has_no_profile(
    fake_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Held like anything else" is an answer; an empty table is not."""
    monkeypatch.setattr(
        fake_client, "grip_profiles_get",
        lambda labware_type: _record(labware_type, MoveParameterPatch()),
    )

    result = runner.invoke(app, ["grip-profiles", "show", "never_measured"])

    assert result.exit_code == 0, result.output
    assert "no grip profile" in result.stderr


def test_set_sends_only_the_fields_the_operator_named(fake_client) -> None:
    result = runner.invoke(
        app, ["grip-profiles", "set", "costar_96", "--resource-width", "76"],
    )

    assert result.exit_code == 0, result.output
    assert fake_client.patched is not None
    labware_type, body = fake_client.patched
    assert labware_type == "costar_96"
    assert body.set.model_dump(exclude_none=True) == {"resource_width": 76.0}
    assert body.clear == []


def test_set_carries_the_fields_being_handed_back(fake_client) -> None:
    result = runner.invoke(
        app, ["grip-profiles", "set", "costar_96", "--clear", "z_offset"],
    )

    assert result.exit_code == 0, result.output
    assert fake_client.patched is not None
    assert fake_client.patched[1].clear == ["z_offset"]


def test_set_with_nothing_named_is_a_usage_error(fake_client) -> None:
    result = runner.invoke(app, ["grip-profiles", "set", "costar_96"])

    assert result.exit_code == output.EXIT_USAGE
    assert fake_client.patched is None


def test_clearing_a_field_that_is_not_a_move_parameter_is_a_usage_error(
    fake_client,
) -> None:
    result = runner.invoke(
        app, ["grip-profiles", "set", "costar_96", "--clear", "grip_height"],
    )

    assert result.exit_code == output.EXIT_USAGE
    assert fake_client.patched is None


def test_reset_names_the_labware_type(fake_client) -> None:
    result = runner.invoke(app, ["grip-profiles", "reset", "costar_96"])

    assert result.exit_code == 0, result.output
    assert fake_client.reset == "costar_96"


def test_set_offers_every_field_the_grip_profile_layer_can_hold() -> None:
    """The CLI names its options one at a time, so a field added to the patch
    model reaches REST for free and stops here.

    The approach fields are excluded on purpose: a teachpoint supplies all four
    on every move and resolves after this layer, so a value set here would be
    overwritten before an arm saw it.
    """
    import inspect

    from cheshire_drivers.move_parameters import MoveParameterPatch
    from orca.cli.grip_profiles import set_grip_profile
    from orca.runtime.move_parameters import SITE_OWNED_FIELDS

    offered = set(inspect.signature(set_grip_profile).parameters) - {
        "labware_type", "clear",
    }
    settable = set(MoveParameterPatch.model_fields) - set(SITE_OWNED_FIELDS)

    assert offered == settable, (
        "`orca grip-profiles set` must offer every field this layer can hold. "
        f"Missing from the CLI: {sorted(settable - offered)}. "
        f"On the CLI but not settable: {sorted(offered - settable)}."
    )
