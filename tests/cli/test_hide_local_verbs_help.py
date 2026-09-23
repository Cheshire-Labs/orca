"""`--help` hides local-only sub-apps under a cloud backend.

Symmetric to the existing `_hide_cloud_verbs_in_help` behavior: a cloud
operator's `--help` should not advertise sub-apps whose every verb is
local-only and would immediately fail-clean.
"""

import sys
from unittest.mock import patch

import pytest


def test_hide_local_when_env_says_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    from orca.cli import app as app_mod

    monkeypatch.setenv("ORCA_BACKEND", "cloud")
    monkeypatch.setattr(sys, "argv", ["orca", "--help"])
    with patch("orca.cli.backend._daemon_reachable", return_value=True):
        assert app_mod._hide_local_verbs_in_help() is True


def test_hide_local_when_flag_is_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    from orca.cli import app as app_mod

    for var in ("ORCA_BACKEND", "ORCA_CLOUD_URL", "ORCA_CLOUD_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["orca", "--backend", "cloud", "--help"])
    assert app_mod._hide_local_verbs_in_help() is True


def test_hide_local_when_cloud_creds_and_no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    from orca.cli import app as app_mod

    monkeypatch.delenv("ORCA_BACKEND", raising=False)
    monkeypatch.setenv("ORCA_CLOUD_URL", "https://orca.example.com")
    monkeypatch.setenv("ORCA_CLOUD_API_KEY", "key")
    monkeypatch.setattr(sys, "argv", ["orca", "--help"])
    with patch("orca.cli.backend._daemon_reachable", return_value=False):
        assert app_mod._hide_local_verbs_in_help() is True


def test_show_local_when_backend_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from orca.cli import app as app_mod

    monkeypatch.setenv("ORCA_BACKEND", "local")
    monkeypatch.setattr(sys, "argv", ["orca", "--help"])
    assert app_mod._hide_local_verbs_in_help() is False


def test_show_local_when_nothing_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """No signal at all -> operator hasn't picked cloud; keep local verbs."""
    from orca.cli import app as app_mod

    for var in ("ORCA_BACKEND", "ORCA_CLOUD_URL", "ORCA_CLOUD_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["orca", "--help"])
    with patch("orca.cli.backend._daemon_reachable", return_value=False):
        assert app_mod._hide_local_verbs_in_help() is False


def test_hide_local_skips_daemon_probe_on_non_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-help invocation must not pay the synchronous /health probe."""
    from orca.cli import app as app_mod

    for var in ("ORCA_BACKEND", "ORCA_CLOUD_URL", "ORCA_CLOUD_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ORCA_CLOUD_URL", "https://orca.example.com")
    monkeypatch.setenv("ORCA_CLOUD_API_KEY", "key")
    monkeypatch.setattr(sys, "argv", ["orca", "device", "list"])
    with patch(
        "orca.cli.backend._daemon_reachable",
        side_effect=AssertionError("probe must not run on non-help"),
    ):
        assert app_mod._hide_local_verbs_in_help() is False
