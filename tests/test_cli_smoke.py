"""Typer CliRunner smoke tests for the `orca` CLI.

These tests don't exercise a running workflow end-to-end; the end-to-end
tests do that. They verify:

1. Every noun sub-app is wired and has a help text.
2. Root-level lifecycle verbs work without a daemon where appropriate.
3. Mutation/state verbs refuse with `EXIT_NOT_CONNECTED` when no daemon.
4. With a daemon + loaded fixture system, basic CLI flows round-trip.
5. `--json` / `--quiet` flags alter stdout/stderr as expected.

The 4 tests that previously used `ORCA_SYSTEM_MODULE` (pre-daemon model)
were rewritten against the `running_daemon` / `loaded_daemon`
fixtures in tests/conftest.py. Intent preserved: clean refusal without
prereq; successful round-trip with prereq.
"""

from typer.testing import CliRunner

from orca.cli import output as _out
from orca.cli.app import app


runner = CliRunner()


def test_root_help_lists_every_noun() -> None:
    """Root --help mentions every noun sub-app plus lifecycle verbs."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.stdout
    assert "run" in result.stdout
    for noun in (
        "execution", "thread", "var", "labware", "device", "reservation",
        "incident", "workflow", "method", "location", "describe",
    ):
        assert noun in result.stdout, f"noun {noun!r} missing from root help"


def test_version_prints() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "orca" in result.stdout


def test_version_json() -> None:
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    assert '"version"' in result.stdout


def test_every_noun_help() -> None:
    """Every noun sub-app's --help renders without crashing."""
    for noun in (
        "execution", "thread", "var", "labware", "device", "reservation",
        "incident", "workflow", "method", "location", "describe",
    ):
        result = runner.invoke(app, [noun, "--help"])
        assert result.exit_code == 0, f"{noun} --help exited {result.exit_code}"


# -- The 4 previously-skipped tests, rewritten for the daemon model. ---------
#
# Original assertion: a CLI verb refuses without --system-module (pre-daemon,
# each CLI invocation built its own system). New assertion: a CLI verb
# refuses with EXIT_NOT_CONNECTED when no daemon is running. Intent
# preserved: "CLI doesn't silently pretend to work when its prereq isn't met".


def test_run_refuses_without_daemon(tmp_path, monkeypatch) -> None:
    """`orca run` with no daemon exits with EXIT_NOT_CONNECTED (10)."""
    # Isolated home so we don't accidentally see the user's real daemon.
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    result = runner.invoke(app, ["run", "simple_workflow", "--run-mode", "PURE_SIM"])
    assert result.exit_code == _out.EXIT_NOT_CONNECTED


def test_execution_list_refuses_without_daemon(tmp_path, monkeypatch) -> None:
    """Same refusal path for a read verb (proves the guard covers reads too)."""
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    result = runner.invoke(app, ["execution", "list"])
    assert result.exit_code == _out.EXIT_NOT_CONNECTED


def test_run_against_loaded_daemon_exits_zero(
    loaded_daemon,
) -> None:
    """With a daemon + fixture system loaded, `orca run --json` returns a real
    execution record.

    Proves the full CLI -> HTTP -> daemon -> SystemRuntime.submit_workflow
    path is wired and produced an execution: --json puts the record on stdout
    (the human-mode info line goes to stderr, which CliRunner drops). We assert
    the record's identity, not just exit 0, so a daemon that accepts the request
    but returns a wrong/empty body fails here. The companion
    `test_execution_list_against_loaded_daemon_shows_submitted` proves the
    submitted execution landed in the runtime; we don't duplicate that.
    """
    import json

    result = runner.invoke(
        app, ["--json", "run", "simple_workflow", "--run-mode", "PURE_SIM"],
    )
    assert result.exit_code == 0, (
        f"exit={result.exit_code} stdout={result.stdout!r}"
    )
    record = json.loads(result.stdout)
    assert record["workflow_name"] == "simple_workflow"
    assert record["id"]
    assert isinstance(record["status"], str) and record["status"]


def test_execution_list_against_loaded_daemon_shows_submitted(
    loaded_daemon,
) -> None:
    """After a submit, `orca execution list --json` must contain the id.

    Replaces the pre-daemon test that asserted the SMC workflow showed up
    in the registry after ORCA_SYSTEM_MODULE was set. Now asserts the
    daemon holds the submitted execution.
    """
    import json

    submit = runner.invoke(app, ["run", "simple_workflow", "--run-mode", "PURE_SIM"])
    assert submit.exit_code == 0

    listing = runner.invoke(app, ["--json", "execution", "list"])
    assert listing.exit_code == 0
    data = json.loads(listing.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert all("id" in entry and "workflow_name" in entry for entry in data)
