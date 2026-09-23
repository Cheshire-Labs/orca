"""CLI polish coverage, in one file so the touch list is easy to audit.

- `_daemon_reachable` is silent on a missing PID file
- `labware edit-barcode` accepts the `--barcode` flag form
- `orca run --wait` timeout message includes a follow-up hint
- `orca status` verb shape (json) and its no-daemon exit
- `_hide_cloud_verbs_in_help` honours backend signals
- `LiveSubmissionWithSimOverridesUnacknowledgedError` truncates the
  inline name list past 5 devices
- `describe device` surfaces `under_external_control`
"""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
import typer
from typer.testing import CliRunner

from orca.daemon.lifecycle import DaemonInfo
from orca.daemon.schemas import HealthResponse
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import (
    LiveSubmissionWithSimOverridesUnacknowledgedError,
)




def test_daemon_reachable_silent_when_no_pid_file(tmp_path) -> None:
    """Probe never prints to stderr on a missing PID file."""
    from orca.cli import backend as backend_mod

    captured = io.StringIO()
    with redirect_stderr(captured), patch.object(
        backend_mod, "detect_live_daemon", return_value=None,
    ):
        result = backend_mod._daemon_reachable()
    assert result is False
    assert captured.getvalue() == ""


def test_daemon_reachable_silent_when_health_fails() -> None:
    """Probe never prints to stderr when /health raises an HTTP error."""
    from orca.cli import backend as backend_mod

    info = DaemonInfo(pid=12345, port=8080, started_at=0.0)
    captured = io.StringIO()
    with redirect_stderr(captured), patch.object(
        backend_mod, "detect_live_daemon", return_value=info,
    ), patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.side_effect = (
            httpx.ConnectError("refused")
        )
        result = backend_mod._daemon_reachable()
    assert result is False
    assert captured.getvalue() == ""




def test_edit_barcode_accepts_flag_form() -> None:
    """--barcode <value> is interchangeable with the positional form."""
    from orca.cli import labware as labware_mod

    mock_client = MagicMock()
    runner = CliRunner()
    with patch.object(labware_mod, "get_client", return_value=mock_client):
        result = runner.invoke(
            labware_mod.app, ["edit-barcode", "lw-1", "--barcode", "BC9"],
        )
    assert result.exit_code == 0, result.output
    mock_client.labware_edit_barcode.assert_called_once_with("lw-1", "BC9")


def test_edit_barcode_accepts_positional_form() -> None:
    """Backward-compat: the positional second arg still works."""
    from orca.cli import labware as labware_mod

    mock_client = MagicMock()
    runner = CliRunner()
    with patch.object(labware_mod, "get_client", return_value=mock_client):
        result = runner.invoke(
            labware_mod.app, ["edit-barcode", "lw-1", "BC-positional"],
        )
    assert result.exit_code == 0, result.output
    mock_client.labware_edit_barcode.assert_called_once_with("lw-1", "BC-positional")


def test_edit_barcode_rejects_conflicting_values() -> None:
    """Both positional and --barcode supplied with different values -> error."""
    from orca.cli import labware as labware_mod

    mock_client = MagicMock()
    runner = CliRunner()
    with patch.object(labware_mod, "get_client", return_value=mock_client):
        result = runner.invoke(
            labware_mod.app,
            ["edit-barcode", "lw-1", "POS", "--barcode", "FLAG"],
        )
    assert result.exit_code != 0
    mock_client.labware_edit_barcode.assert_not_called()


def test_edit_barcode_rejects_missing_value() -> None:
    """Neither positional nor flag supplied -> error."""
    from orca.cli import labware as labware_mod

    mock_client = MagicMock()
    runner = CliRunner()
    with patch.object(labware_mod, "get_client", return_value=mock_client):
        result = runner.invoke(labware_mod.app, ["edit-barcode", "lw-1"])
    assert result.exit_code != 0




@pytest.fixture
def a_cloud_backend_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend signals below only matter on a build that has a cloud backend."""
    monkeypatch.setattr("orca.cli.backend.cloud_backend_installed", lambda: True)


@pytest.mark.usefixtures("a_cloud_backend_is_installed")
def test_hide_cloud_verbs_when_local_daemon_reachable(monkeypatch) -> None:
    """No backend signal + reachable local daemon -> hide cloud verbs."""
    from orca.cli import app as app_mod

    for var in ("ORCA_BACKEND", "ORCA_CLOUD_URL", "ORCA_CLOUD_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["orca", "--help"])
    with patch("orca.cli.backend._daemon_reachable", return_value=True):
        assert app_mod._hide_cloud_verbs_in_help() is True


@pytest.mark.usefixtures("a_cloud_backend_is_installed")
def test_show_cloud_verbs_when_env_says_cloud(monkeypatch) -> None:
    """ORCA_BACKEND=cloud -> show cloud verbs even when daemon present."""
    from orca.cli import app as app_mod

    monkeypatch.setenv("ORCA_BACKEND", "cloud")
    monkeypatch.setattr(sys, "argv", ["orca", "--help"])
    with patch("orca.cli.backend._daemon_reachable", return_value=True):
        assert app_mod._hide_cloud_verbs_in_help() is False


@pytest.mark.usefixtures("a_cloud_backend_is_installed")
def test_show_cloud_verbs_when_flag_is_cloud(monkeypatch) -> None:
    """--backend cloud in argv -> show cloud verbs."""
    from orca.cli import app as app_mod

    for var in ("ORCA_BACKEND", "ORCA_CLOUD_URL", "ORCA_CLOUD_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["orca", "--backend", "cloud", "incident", "--help"])
    with patch("orca.cli.backend._daemon_reachable", return_value=True):
        assert app_mod._hide_cloud_verbs_in_help() is False


@pytest.mark.usefixtures("a_cloud_backend_is_installed")
def test_show_cloud_verbs_when_cloud_creds_set(monkeypatch) -> None:
    """ORCA_CLOUD_URL + ORCA_CLOUD_API_KEY set -> show cloud verbs."""
    from orca.cli import app as app_mod

    monkeypatch.delenv("ORCA_BACKEND", raising=False)
    monkeypatch.setenv("ORCA_CLOUD_URL", "https://orca.example.com")
    monkeypatch.setenv("ORCA_CLOUD_API_KEY", "key")
    monkeypatch.setattr(sys, "argv", ["orca", "--help"])
    with patch("orca.cli.backend._daemon_reachable", return_value=False):
        assert app_mod._hide_cloud_verbs_in_help() is False




def test_live_overrides_error_truncates_long_device_list() -> None:
    """33 devices -> first 5 names inline + remainder summary."""
    devices = [
        (f"dev_{i}", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM)
        for i in range(33)
    ]
    exc = LiveSubmissionWithSimOverridesUnacknowledgedError(devices=devices)
    msg = str(exc)
    # First 5 inline
    for i in range(5):
        assert f"dev_{i}" in msg
    # 6th onward NOT inline
    assert "dev_5" not in msg
    assert "and 28 more" in msg
    assert "orca device list" in msg


def test_live_overrides_error_keeps_short_list_inline() -> None:
    """<=5 devices -> all names inline, no truncation."""
    devices = [
        (f"d{i}", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM)
        for i in range(3)
    ]
    exc = LiveSubmissionWithSimOverridesUnacknowledgedError(devices=devices)
    msg = str(exc)
    for i in range(3):
        assert f"d{i}" in msg
    assert "and " not in msg or "device list" not in msg


def test_live_overrides_error_exposes_full_typed_list() -> None:
    """Truncation only affects the message string; typed `devices` list is full."""
    devices = [
        (f"d_{i}", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM)
        for i in range(20)
    ]
    exc = LiveSubmissionWithSimOverridesUnacknowledgedError(devices=devices)
    assert len(exc.devices) == 20




def test_status_emits_payload_when_daemon_running() -> None:
    """`orca status` returns JSON with daemon + health fields."""
    from orca.cli import status as status_mod
    from orca.cli import output as output_mod

    info = DaemonInfo(pid=42, port=8000, started_at=1000.0)
    health = HealthResponse(
        daemon="ok",
        system_loaded=True,
        runtime_state=None,
        spec="examples.smc_assay.smc_assay_example:build_smc",
        sim=True,
    )
    captured = io.StringIO()
    output_mod.set_mode(output_mod.OutputMode.JSON)
    try:
        with redirect_stdout(captured), patch.object(
            status_mod, "detect_live_daemon", return_value=info,
        ), patch.object(status_mod, "_fetch_health", return_value=health):
            status_mod.status()
    finally:
        output_mod.set_mode(output_mod.OutputMode.TABLE)
    assert '"pid": 42' in captured.getvalue()
    assert '"port": 8000' in captured.getvalue()
    assert '"system_loaded": true' in captured.getvalue()
    assert '"sim": true' in captured.getvalue()


def test_status_exits_when_no_daemon() -> None:
    """`orca status` exits EXIT_NOT_CONNECTED when no PID file."""
    from orca.cli import status as status_mod

    with patch.object(status_mod, "detect_live_daemon", return_value=None):
        with pytest.raises(typer.Exit) as excinfo:
            status_mod.status()
    assert excinfo.value.exit_code == 10  # EXIT_NOT_CONNECTED


def test_status_marks_health_unreachable_distinctly_from_unloaded() -> None:
    """A /health probe failure must not be confused with
    `system_loaded=False`. We emit `health_reachable=False` and null out
    the loaded / spec / sim fields rather than fabricating defaults."""
    from orca.cli import status as status_mod
    from orca.cli import output as output_mod

    info = DaemonInfo(pid=42, port=8000, started_at=1000.0)
    captured = io.StringIO()
    output_mod.set_mode(output_mod.OutputMode.JSON)
    try:
        with redirect_stdout(captured), patch.object(
            status_mod, "detect_live_daemon", return_value=info,
        ), patch.object(status_mod, "_fetch_health", return_value=None):
            status_mod.status()
    finally:
        output_mod.set_mode(output_mod.OutputMode.TABLE)
    out = captured.getvalue()
    assert '"health_reachable": false' in out
    assert '"system_loaded": null' in out
    assert '"sim": null' in out




def test_describe_device_surfaces_under_external_control() -> None:
    """describe device's kv output includes under_external_control flag."""
    from orca.cli import describe as describe_mod
    from orca.cli import output as output_mod
    from orca.daemon.schemas import DeviceDTO

    snap = DeviceDTO(
        name="shaker_1",
        type_name="Shaker",
        is_initialized=True,
        is_busy=False,
        effective_mode=WorkflowRunMode.PURE_SIM,
        position_ids=("pad1",),
        loaded_labware_ids=(),
        under_external_control=True,
    )
    mock_client = MagicMock()
    mock_client.device_info.return_value = snap
    captured = io.StringIO()
    output_mod.set_mode(output_mod.OutputMode.TABLE)
    with redirect_stdout(captured), patch.object(
        describe_mod, "local_client", return_value=mock_client,
    ):
        describe_mod.describe_device("shaker_1")
    assert "under_external_control" in captured.getvalue()
    assert "True" in captured.getvalue()




def test_run_wait_timeout_message_includes_followup_pointer() -> None:
    """Static assert: the timeout-fail call site builds a message with the
    operator-actionable follow-up text. Done as a source scan rather than
    a runtime drive because `run` imports its client lazily (`get_client`
    inside the function body) and Typer's CliRunner stdin handling is
    fiddly to compose with that."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "orca" / "cli" / "app.py"
    body = src.read_text(encoding="utf-8")
    assert "Execution is still running" in body
    assert "orca execution detail" in body
    assert "--timeout" in body


def test_run_wait_timeout_hint_uses_full_execution_id() -> None:
    """The hint must paste cleanly, so the id is full, not [:8]."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "orca" / "cli" / "app.py"
    body = src.read_text(encoding="utf-8")
    # full id used in the suggested command
    assert "orca execution detail {record.id}" in body
    # the 8-char prefix is fine as a display label but never in a pastable command
    assert "orca execution detail {record.id[:8]}" not in body


def test_backend_signal_from_argv_handles_equals_form(monkeypatch) -> None:
    """`--backend=cloud` is parsed the same as `--backend cloud`."""
    from orca.cli import app as app_mod

    monkeypatch.setattr(sys, "argv", ["orca", "--backend=cloud", "module"])
    assert app_mod._backend_signal_from_argv() == "cloud"

    monkeypatch.setattr(sys, "argv", ["orca", "--backend=local", "labware", "list"])
    assert app_mod._backend_signal_from_argv() == "local"

    monkeypatch.setattr(sys, "argv", ["orca", "--backend", "cloud", "module"])
    assert app_mod._backend_signal_from_argv() == "cloud"

    monkeypatch.setattr(sys, "argv", ["orca", "labware", "list"])
    assert app_mod._backend_signal_from_argv() is None


def test_hide_cloud_skips_daemon_probe_on_non_help_invocation(monkeypatch) -> None:
    """Non-help commands MUST NOT touch the daemon probe.

    The probe can synchronously hit /health; if we did it on every CLI
    invocation we'd add 100ms+ to every command. Only --help paths
    should pay that cost.
    """
    from orca.cli import app as app_mod

    for var in ("ORCA_BACKEND", "ORCA_CLOUD_URL", "ORCA_CLOUD_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["orca", "labware", "list"])
    with patch(
        "orca.cli.backend._daemon_reachable",
        side_effect=AssertionError("should not be called on non-help invocations"),
    ):
        # No raise = no probe touched
        assert app_mod._hide_cloud_verbs_in_help() is False
