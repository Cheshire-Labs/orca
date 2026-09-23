"""`--help` tells the operator where specs resolve and which backend a verb needs.

The daemon imports a `module:function` spec, not the CLI, so a module resolves
from the directory `orca start` ran in. Nothing in `--help` said so, and a
mount run from another directory failed with "module not found". Three
sub-apps also called themselves cloud-only while their verbs run against the
local daemon too.
"""

import re
import sys

import pytest
from typer.testing import CliRunner

from orca.cli import app as app_module
from orca.cli import backend
from orca.cli.app import app
from orca.cli.backend import cloud_backend_installed

runner = CliRunner()


def _help(argv: list[str]) -> str:
    """The help text as one line, with the panel borders Rich draws removed."""
    result = runner.invoke(app, [*argv, "--help"])
    assert result.exit_code == 0, result.output
    return " ".join(re.sub(r"[─-╿]", " ", result.output).split())


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["start"], "Run it from your project root"),
        (["topology", "mount"], "imports from the directory `orca start` ran in"),
        (["workflow", "load"], "imports from the directory `orca start` ran in"),
    ],
)
def test_help_says_the_daemon_imports_specs_from_where_orca_start_ran(
    argv: list[str], says: str,
) -> None:
    assert says in _help(argv)


@pytest.mark.parametrize("argv", [["ops-history"], ["incident", "recoverable-timeout"]])
def test_help_does_not_call_a_sub_app_that_runs_locally_cloud_only(argv: list[str]) -> None:
    text = _help(argv).lower()
    assert "cloud-only" not in text
    assert "cloud only" not in text


def test_runtime_help_does_not_call_status_cloud_only() -> None:
    text = _help(["runtime"]).lower()
    assert "runtime status" in text
    assert "cloud-only" not in text
    assert "cloud only" not in text


@pytest.mark.parametrize("argv", [["topology", "mount"], ["workflow", "load"]])
def test_mount_and_load_help_mention_the_cloud_only_on_a_build_that_has_it(argv: list[str]) -> None:
    """The public build has no cloud backend, so its help must not offer one."""
    assert ("cloud" in _help(argv).lower()) is cloud_backend_installed()


def test_with_no_cloud_backend_the_cloud_only_verbs_are_hidden_from_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["orca", "--backend", "cloud", "--help"])
    monkeypatch.setattr(backend, "cloud_backend_installed", lambda: False)

    assert app_module._hide_cloud_verbs_in_help() is True


def test_with_a_cloud_backend_asking_for_the_cloud_shows_its_verbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["orca", "--backend", "cloud", "--help"])
    monkeypatch.setattr(backend, "cloud_backend_installed", lambda: True)

    assert app_module._hide_cloud_verbs_in_help() is False
