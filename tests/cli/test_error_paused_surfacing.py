"""CLI surfacing of error-paused threads.

A default-PAUSE action failure parks the thread and leaves the execution
in ACCEPTING. The error is on `ExecutionDetailDTO.threads[].last_error`,
but the table render and `--wait` poll used to ignore it. These tests
pin the surfacing:

- `orca execution detail` prints a per-thread PAUSED(error) line.
- `--wait` detection returns the error-paused threads so the caller can
  fail fast with an actionable message instead of a blind timeout.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import orca.cli.app  # noqa: F401  -- registers sub-apps, breaks the import cycle
from orca.cli import execution as execution_mod
from orca.cli import output as output_mod
from orca.cli.control_plane import ExecutionDetailDTO, ThreadSnapshotDTO


def _detail_with_error_paused(
    description: str = "Shakes the plate for 30 minutes, then reads it.",
) -> ExecutionDetailDTO:
    return ExecutionDetailDTO(
        id="exec-1234abcd",
        workflow_name="smc_assay",
        status="accepting",
        error=None,
        threads=[
            ThreadSnapshotDTO(
                id="thread-aaaa1111",
                name="plate_1_journey",
                status="PAUSED",
                last_error="RuntimeError: Simulated shake failure",
                pause_reason="error",
                current_method={
                    "name": "shake_and_read",
                    "current_action": {
                        "command": "run_target_capture",
                        "description": description,
                    },
                },
            ),
            ThreadSnapshotDTO(
                id="thread-bbbb2222",
                name="plate_2_journey",
                status="RESOLVING_ACTION_LOCATION",
            ),
        ],
        total_thread_count=2,
        completed_thread_count=0,
        active_thread_count=1,
    )


def _detail_all_running() -> ExecutionDetailDTO:
    return ExecutionDetailDTO(
        id="exec-1234abcd",
        workflow_name="smc_assay",
        status="accepting",
        threads=[
            ThreadSnapshotDTO(id="t1", name="j1", status="RUNNING"),
        ],
        total_thread_count=1,
        active_thread_count=1,
    )


def test_error_paused_threads_helper_picks_error_pauses() -> None:
    paused = execution_mod.error_paused_threads(_detail_with_error_paused())
    assert [t.id for t in paused] == ["thread-aaaa1111"]
    assert paused[0].last_error == "RuntimeError: Simulated shake failure"


def test_error_paused_threads_helper_empty_when_all_running() -> None:
    assert execution_mod.error_paused_threads(_detail_all_running()) == []


def test_detail_render_shows_per_thread_error_pause() -> None:
    client = MagicMock()
    client.list_executions.return_value = [
        MagicMock(id="exec-1234abcd"),
    ]
    client.get_execution.return_value = _detail_with_error_paused()

    output_mod.set_mode(output_mod.OutputMode.TABLE)
    captured = io.StringIO()
    with redirect_stdout(captured), patch.object(
        execution_mod, "get_client", return_value=client,
    ):
        execution_mod.detail("exec-1234abcd")

    out = captured.getvalue()
    assert "PAUSED (error)" in out
    assert "Simulated shake failure" in out
    assert "plate_1_journey" in out


def _run_record() -> MagicMock:
    rec = MagicMock()
    rec.id = "exec-1234abcd"
    rec.workflow_name = "smc_assay"
    return rec


def test_run_wait_fails_fast_on_error_paused_thread() -> None:
    """`run --wait` must not blind-timeout when a thread is error-paused.

    The execution stays ACCEPTING (a paused thread is non-terminal), so
    the terminal-state poll would spin until --timeout. Detection breaks
    the poll with a non-zero exit + an actionable recover hint.
    """
    from orca.cli import app as app_mod
    from orca.cli import backend as backend_mod

    client = MagicMock()
    client.submit_workflow.return_value = _run_record()
    client.get_execution.return_value = _detail_with_error_paused()

    output_mod.set_mode(output_mod.OutputMode.TABLE)
    captured_err = io.StringIO()
    import contextlib
    with patch.object(backend_mod, "get_client", return_value=client), \
            patch.object(backend_mod, "active_backend", return_value="local"), \
            patch("orca.cli.resolve.record_last_execution"), \
            contextlib.redirect_stderr(captured_err):
        import typer
        try:
            app_mod.run(
                "smc_assay",
                wait=True,
                vars_="",
                profile="",
                timeout=5.0,
                poll=0.01,
                run_mode="PURE_SIM",
                confirm=False,
            )
        except typer.Exit as exc:
            assert exc.exit_code not in (0, None), "expected non-zero exit"
        else:
            raise AssertionError("run --wait should have exited non-zero")

    err = captured_err.getvalue()
    assert "error-paused" in err.lower()
    assert "recover" in err.lower()


def _rendered(detail: ExecutionDetailDTO) -> str:
    client = MagicMock()
    client.list_executions.return_value = [MagicMock(id="exec-1234abcd")]
    client.get_execution.return_value = detail
    output_mod.set_mode(output_mod.OutputMode.TABLE)
    captured = io.StringIO()
    with redirect_stdout(captured), patch.object(
        execution_mod, "get_client", return_value=client,
    ):
        execution_mod.detail("exec-1234abcd")
    return captured.getvalue()


def test_the_action_is_shown_in_the_authors_own_words() -> None:
    """The name of the action is on the row already; the sentence is the point."""
    out = _rendered(_detail_with_error_paused())

    assert "run_target_capture" in out
    assert "Shakes the plate for 30 minutes" in out


def test_a_bracketed_word_in_a_docstring_survives_to_the_screen() -> None:
    """Docstrings are prose an author wrote, and prose contains brackets.

    The renderer reads its own markup out of what it prints, so an unescaped
    value loses a bracketed word silently and can refuse to print at all.
    """
    out = _rendered(_detail_with_error_paused("Move the [sample] plate to the reader."))

    assert "[sample]" in out


def test_a_docstring_that_looks_like_a_closing_tag_still_prints() -> None:
    out = _rendered(_detail_with_error_paused("Drop tips [/] then home."))

    assert "Drop tips [/] then home." in out


def test_a_multi_line_docstring_stays_on_its_row() -> None:
    """A paragraph printed into a key/value block runs flush left and breaks it."""
    out = _rendered(
        _detail_with_error_paused("First line.\n\n    Second paragraph, indented.")
    )

    assert "First line. Second paragraph, indented." in out
