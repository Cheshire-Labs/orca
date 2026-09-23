"""Backend resolution precedence, and symmetric fail-clean."""

import json
import os
from collections.abc import Iterator
from importlib.metadata import entry_points
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

import orca.cli.backend as backend_mod
from orca.cli.app import STATE
from orca.cli.backend import (
    _CLOUD_REDIRECT,
    _LOCAL_REDIRECT,
    make_client,
    require_cloud,
    require_local,
    resolve_backend,
)
from orca.cli.client import LocalDaemonClient
from orca.cli.control_plane import (
    BackendNotResolvedError,
    ControlPlaneError,
    ICloudControlPlaneClient,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path) -> Iterator[None]:
    """Reset CLI state and stub HOME so config.json from the user does not leak."""
    STATE.backend = None
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    with patch.object(Path, "home", return_value=fake_home):
        for var in (
            "ORCA_BACKEND",
            "ORCA_CLOUD_URL",
            "ORCA_CLOUD_API_KEY",
        ):
            os.environ.pop(var, None)
        yield


def test_flag_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6 precedence: flag > env > config > auto."""
    monkeypatch.setenv("ORCA_BACKEND", "cloud")
    assert resolve_backend("local") == "local"


def test_env_wins_over_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Env var beats config file."""
    monkeypatch.setenv("ORCA_BACKEND", "cloud")
    cfg = Path.home() / ".orca"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text(json.dumps({"backend": "local"}))
    assert resolve_backend(None) == "cloud"


def test_config_wins_over_auto() -> None:
    """Config file picks the backend even when no env is set."""
    cfg = Path.home() / ".orca"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text(json.dumps({"backend": "cloud"}))
    assert resolve_backend(None) == "cloud"


def test_auto_falls_back_to_local_when_daemon_reachable() -> None:
    with patch.object(backend_mod, "_daemon_reachable", return_value=True):
        with patch.object(backend_mod, "_has_cloud_creds", return_value=False):
            assert resolve_backend(None) == "local"


def test_daemon_reachable_returns_false_when_no_pid_file() -> None:
    """Regression: `LocalDaemonClient.__init__` raises `typer.Exit` (NOT
    `SystemExit`) when the PID file is missing. `_daemon_reachable` must
    catch both so auto-detect can fall through to the helpful error message
    instead of aborting the CLI on the typer.Exit.
    """
    # _isolate_state autouse fixture stubs Path.home() to an empty fake dir,
    # so no daemon.json exists -> LocalDaemonClient.__init__ raises typer.Exit.
    assert backend_mod._daemon_reachable() is False


def test_auto_picks_cloud_when_daemon_down_and_creds_present() -> None:
    with patch.object(backend_mod, "_daemon_reachable", return_value=False):
        with patch.object(backend_mod, "_has_cloud_creds", return_value=True):
            assert resolve_backend(None) == "cloud"


def test_invalid_flag_value_raises() -> None:
    with pytest.raises(ControlPlaneError):
        resolve_backend("staging")


def test_invalid_env_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_BACKEND", "azure")
    with pytest.raises(ControlPlaneError):
        resolve_backend(None)


def test_invalid_config_value_raises() -> None:
    cfg = Path.home() / ".orca"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text(json.dumps({"backend": "azure"}))
    with pytest.raises(ControlPlaneError):
        resolve_backend(None)


def test_make_client_local_returns_local_daemon_client() -> None:
    with patch.object(LocalDaemonClient, "__init__", lambda self, *a, **k: None):
        client = make_client("local")
    assert isinstance(client, LocalDaemonClient)


class _NeverBuilt:
    """The cloud client of an installed backend. Missing credentials must stop the CLI first."""

    def __init__(self, base_url: str, api_key: str) -> None:
        raise AssertionError("the cloud client was built without credentials")


class _RegisteredCloudBackend:
    """The entry point an installed cloud backend registers."""

    def load(self) -> type[_NeverBuilt]:
        return _NeverBuilt


@pytest.fixture
def a_cloud_backend_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public build registers none, and without one the credentials are never read."""
    monkeypatch.setattr(backend_mod, "entry_points", lambda **kwargs: (_RegisteredCloudBackend(),))


@pytest.mark.usefixtures("a_cloud_backend_is_installed")
def test_make_client_cloud_requires_url_and_key() -> None:
    """No silent fallback: missing creds raises."""
    with pytest.raises(ControlPlaneError) as excinfo:
        make_client("cloud")
    assert "ORCA_CLOUD_URL" in str(excinfo.value)
    assert "ORCA_CLOUD_API_KEY" in str(excinfo.value)


def test_make_client_cloud_answers_for_whichever_build_this_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With creds set, a build that registers a cloud backend constructs it,
    and a build that registers none says so. Both are correct; which one you
    get is the difference between the two builds."""
    monkeypatch.setenv("ORCA_CLOUD_URL", "https://example")
    monkeypatch.setenv("ORCA_CLOUD_API_KEY", "secret")

    if list(entry_points(group="orca.cli_backends", name="cloud")):
        client = make_client("cloud")
        assert isinstance(client, ICloudControlPlaneClient)
        assert client.base_url == "https://example"
    else:
        with pytest.raises(typer.Exit):
            make_client("cloud")


@pytest.mark.usefixtures("a_cloud_backend_is_installed")
def test_no_silent_fallback_cloud_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--backend cloud` without API key fails fast; never silently uses local."""
    monkeypatch.setenv("ORCA_CLOUD_URL", "https://example")
    monkeypatch.delenv("ORCA_CLOUD_API_KEY", raising=False)
    assert resolve_backend("cloud") == "cloud"  # resolution still says cloud
    with pytest.raises(ControlPlaneError):
        make_client("cloud")  # construction is what fails


def _strip_wrap(text: str) -> str:
    """Collapse soft-wrapped output back to a single line for substring asserts."""
    return " ".join(text.split())


def test_require_local_exits_on_cloud_backend(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric fail-clean (D4): local-only verb on cloud emits redirect."""
    import typer
    monkeypatch.setenv("ORCA_BACKEND", "cloud")
    monkeypatch.setenv("ORCA_CLOUD_URL", "https://example")
    monkeypatch.setenv("ORCA_CLOUD_API_KEY", "secret")
    with pytest.raises(typer.Exit) as excinfo:
        require_local()
    assert excinfo.value.exit_code == 2
    captured = capsys.readouterr()
    combined = _strip_wrap(captured.err + captured.out)
    assert _LOCAL_REDIRECT in combined


def test_require_cloud_exits_on_local_backend(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric fail-clean (D4): cloud-only verb on local emits redirect."""
    import typer
    monkeypatch.setattr(backend_mod, "cloud_backend_installed", lambda: True)
    with patch.object(backend_mod, "_daemon_reachable", return_value=True):
        with pytest.raises(typer.Exit) as excinfo:
            require_cloud()
        assert excinfo.value.exit_code == 2
        captured = capsys.readouterr()
        combined = _strip_wrap(captured.err + captured.out)
        assert _CLOUD_REDIRECT in combined


def test_with_no_cloud_backend_a_cloud_only_verb_names_the_missing_backend(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect tells the reader to pass `--backend cloud`, which cannot work on this build."""
    monkeypatch.setattr(backend_mod, "cloud_backend_installed", lambda: False)
    with patch.object(backend_mod, "_daemon_reachable", return_value=True):
        with pytest.raises(typer.Exit) as excinfo:
            require_cloud()
    assert excinfo.value.exit_code == 2
    captured = capsys.readouterr()
    combined = _strip_wrap(captured.err + captured.out)
    assert "no cloud backend is installed" in combined
    assert _CLOUD_REDIRECT not in combined


def test_require_local_succeeds_silently_on_local_backend() -> None:
    with patch.object(backend_mod, "_daemon_reachable", return_value=True):
        require_local()  # does not exit


def _no_backend_message(monkeypatch: pytest.MonkeyPatch, installed: bool) -> str:
    """What `resolve_backend` says with no daemon, no credentials and no explicit signal."""
    monkeypatch.setattr(backend_mod, "cloud_backend_installed", lambda: installed)
    with patch.object(backend_mod, "_daemon_reachable", return_value=False):
        with pytest.raises(BackendNotResolvedError) as excinfo:
            resolve_backend(None)
    return str(excinfo.value)


def test_with_no_cloud_backend_the_no_backend_message_only_offers_orca_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting cloud credentials changes nothing on a build that has no cloud backend."""
    message = _no_backend_message(monkeypatch, installed=False)
    assert "orca start" in message
    assert "ORCA_CLOUD_URL" not in message


def test_with_a_cloud_backend_the_no_backend_message_offers_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _no_backend_message(monkeypatch, installed=True)
    assert "orca start" in message
    assert "ORCA_CLOUD_URL" in message
