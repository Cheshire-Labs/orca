"""Smoke tests for the new `orca thread <op>` and `orca audit list` verbs.

Argument parsing + usage-error paths only. Happy-path requires a live
daemon with a paused thread, which the daemon E2E suite covers.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli import execution as execution_mod


runner = CliRunner()


_THREAD_PATH: tuple[str, ...] = ("execution", "thread")

_EXEC_ID = "exec-0001"
_THREAD_ID = "thread-0001"


def _mock_client() -> MagicMock:
    """A client whose list/detail snapshots let prefix resolution land on
    `_EXEC_ID` / `_THREAD_ID` so the verb body reaches the mutation call."""
    client = MagicMock()
    client.list_executions.return_value = [MagicMock(id=_EXEC_ID)]
    client.get_execution.return_value = MagicMock(
        threads=[MagicMock(id=_THREAD_ID)],
    )
    return client


def test_skip_method_requires_method_name() -> None:
    """No --method-name -> usage error.

    L1 dropped the --method-id flag because skip-by-id never resolved
    at the lane (mutations work by name; the lane is name-keyed).
    --method-name is now mandatory; first-match-wins on the lane.
    """
    result = runner.invoke(
        app,
        list(_THREAD_PATH) + ["skip-method", "e1", "t1", "--reason", "test"],
    )
    assert result.exit_code != 0
    assert "method-name" in result.output.lower() or "method_name" in result.output.lower()


def test_insert_method_requires_template_or_file() -> None:
    """Neither --template nor --from-file -> usage error."""
    result = runner.invoke(
        app,
        list(_THREAD_PATH) + [
            "insert-method", "e1", "t1", "--where", "tail", "--reason", "test",
        ],
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower()


def test_insert_method_rejects_both_template_and_file() -> None:
    result = runner.invoke(app, list(_THREAD_PATH) + [
        "insert-method", "e1", "t1",
        "--where", "tail",
        "--template", "incubate",
        "--from-file", "/no/such/file.py",
        "--reason", "test",
    ])
    assert result.exit_code != 0


# Typer reports a missing required option / bad option value as exit code 2
# (usage error). That is distinct from a resolved-but-failed command (e.g. the
# "no backend resolved" path is exit 10), so exit==2 specifically pins that the
# command was rejected at argument validation -- the Rich usage panel wraps text
# unpredictably, so we assert the code rather than substring-match the panel.
def test_insert_method_requires_where() -> None:
    """--where is required: omitting it is a usage error, not a silent tail."""
    result = runner.invoke(
        app,
        list(_THREAD_PATH) + [
            "insert-method", "e1", "t1", "--template", "incubate", "--reason", "test",
        ],
    )
    assert result.exit_code == 2


def test_insert_action_requires_where() -> None:
    result = runner.invoke(
        app,
        list(_THREAD_PATH) + [
            "insert-action", "e1", "t1",
            "--from-file", "/no/such/file.py", "--reason", "test",
        ],
    )
    assert result.exit_code == 2


def test_insert_method_rejects_unknown_where() -> None:
    """A --where value outside head|tail|before|after fails fast at the CLI,
    before any backend resolution."""
    result = runner.invoke(
        app,
        list(_THREAD_PATH) + [
            "insert-method", "e1", "t1",
            "--where", "middle", "--template", "incubate", "--reason", "test",
        ],
    )
    assert result.exit_code == 2


def test_skip_action_requires_command_or_id() -> None:
    result = runner.invoke(
        app,
        list(_THREAD_PATH) + ["skip-action", "e1", "t1", "--reason", "test"],
    )
    assert result.exit_code != 0


def test_audit_list_help_renders() -> None:
    """Smoke: the audit subapp at least parses + shows help."""
    result = runner.invoke(app, ["audit", "--help"])
    assert result.exit_code == 0
    assert "audit trail" in result.output.lower()


def test_skip_method_invokes_client_with_parsed_args() -> None:
    """`skip-method` reaches the client mutation with name+reason and reports it."""
    client = _mock_client()
    with patch.object(execution_mod, "get_client", return_value=client):
        result = runner.invoke(app, ["--force", *_THREAD_PATH, "skip-method",
                                     _EXEC_ID, _THREAD_ID,
                                     "--method-name", "incubate", "--reason", "stuck"])
    assert result.exit_code == 0, result.output
    client.thread_skip_method.assert_called_once_with(
        _EXEC_ID, _THREAD_ID, method_name="incubate", reason="stuck",
    )
    assert "skipped method 'incubate'" in result.output


def test_abort_method_invokes_client_with_parsed_args() -> None:
    client = _mock_client()
    with patch.object(execution_mod, "get_client", return_value=client):
        result = runner.invoke(app, ["--force", *_THREAD_PATH, "abort-method",
                                     _EXEC_ID, _THREAD_ID,
                                     "--method-name", "shake", "--reason", "stuck"])
    assert result.exit_code == 0, result.output
    client.thread_abort_method.assert_called_once_with(
        _EXEC_ID, _THREAD_ID, method_name="shake", reason="stuck",
    )
    assert "aborted method 'shake'" in result.output


def test_insert_method_invokes_client_with_parsed_args() -> None:
    """`insert-method --template` parses --where to the InsertWhere literal."""
    client = _mock_client()
    with patch.object(execution_mod, "get_client", return_value=client):
        result = runner.invoke(app, ["--force", *_THREAD_PATH, "insert-method",
                                     _EXEC_ID, _THREAD_ID,
                                     "--template", "wash", "--where", "tail",
                                     "--reason", "recover"])
    assert result.exit_code == 0, result.output
    client.thread_insert_method.assert_called_once_with(
        _EXEC_ID, _THREAD_ID, template_name="wash", method_code=None,
        where="tail", anchor=None, reason="recover",
    )
    assert "inserted method 'wash'" in result.output


def test_skip_action_invokes_client_with_parsed_args() -> None:
    client = _mock_client()
    with patch.object(execution_mod, "get_client", return_value=client):
        result = runner.invoke(app, ["--force", *_THREAD_PATH, "skip-action",
                                     _EXEC_ID, _THREAD_ID,
                                     "--action-command", "aspirate", "--reason", "stuck"])
    assert result.exit_code == 0, result.output
    client.thread_skip_action.assert_called_once_with(
        _EXEC_ID, _THREAD_ID, action_id=None, action_command="aspirate", reason="stuck",
    )
    assert "skipped action 'aspirate'" in result.output


def test_insert_action_invokes_client_with_source_from_file(tmp_path: Path) -> None:
    """`insert-action` reads the source file and forwards its contents."""
    src = tmp_path / "act.py"
    src.write_text("@orca.action\ndef a():\n    pass\n", encoding="utf-8")
    client = _mock_client()
    with patch.object(execution_mod, "get_client", return_value=client):
        result = runner.invoke(app, ["--force", *_THREAD_PATH, "insert-action",
                                     _EXEC_ID, _THREAD_ID, str(src),
                                     "--where", "head", "--reason", "recover"])
    assert result.exit_code == 0, result.output
    client.thread_insert_action.assert_called_once_with(
        _EXEC_ID, _THREAD_ID, action_code=src.read_text(encoding="utf-8"),
        where="head", anchor=None, reason="recover",
    )
    assert "inserted action" in result.output
