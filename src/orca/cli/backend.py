"""Which server the orca CLI talks to: the `local` daemon or a `cloud` deployment.

Resolution precedence:

1. `--backend local|cloud` flag  (parsed in `app.py`)
2. `ORCA_BACKEND=local|cloud`    env var
3. `~/.orca/config.json`         top-level `"backend": "local"` field
4. Auto-fallback: probe `~/.orca/daemon.json` + daemon `health()`. Pick `local`
   on probe success; on failure, say to run `orca start` or set the cloud variables.

(The config file is JSON rather than TOML to stay on the standard library:
`requires-python = ">=3.10"` predates `tomllib`, and pulling in `tomli` for
one config field is not worth the dependency.)

NO silent cross-backend fallback. If the operator asks for `cloud` and the
required env vars are missing, this module raises with an actionable message
instead of degrading to `local`. Symmetric: `--backend local` with no daemon
running fails clearly rather than silently dialing the cloud.

Symmetric fail-clean dispatch for command classification mismatches lives
in `require_local()` / `require_cloud()`. The CLI verb body calls one of those
at its top before doing real work; mismatch exits non-zero with a documented
redirect message.
"""

import functools
import json
import os
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal


from orca.cli import output
from orca.cli.daemon_client import DaemonLoadingClient
from orca.cli.client import OrcaClient
from orca.cli.control_plane import (
    ENV_CLOUD_API_KEY,
    BackendNotResolvedError,
    ControlPlaneError,
    ICloudControlPlaneClient,
    IControlPlaneClient,
)
from orca.daemon.lifecycle import detect_live_daemon, pid_file_path


BackendName = Literal["local", "cloud"]

# Setuptools entry-point group for the package that implements the hosted-only
# verbs. Every other verb reaches a hosted server through `OrcaClient`.
_CLI_BACKENDS_GROUP = "orca.cli_backends"


_VALID_BACKENDS: frozenset[str] = frozenset({"local", "cloud"})

_ENV_BACKEND = "ORCA_BACKEND"
_ENV_CLOUD_URL = "ORCA_CLOUD_URL"
_ENV_CLOUD_API_KEY = ENV_CLOUD_API_KEY

_LOCAL_REDIRECT = (
    "this command requires a local orca daemon; "
    "run with --backend local or set ORCA_BACKEND=local"
)
_CLOUD_REDIRECT = (
    "this command requires a cloud deployment; "
    "run with --backend cloud or set ORCA_BACKEND=cloud"
)
_NO_CLOUD_BACKEND = (
    "no cloud backend is installed; --backend cloud needs a package that "
    f"registers entry-point group '{_CLI_BACKENDS_GROUP}' name 'cloud'"
)


def _config_path() -> Path:
    return Path.home() / ".orca" / "config.json"


def _read_config_backend() -> BackendName | None:
    """Read top-level `"backend"` from `~/.orca/config.json` if present.

    Missing file or missing field returns None. Malformed JSON / unknown
    backend value raises ControlPlaneError so the operator sees the typo
    instead of a silent fallback.
    """
    path = _config_path()
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlPlaneError(
            f"could not read {path}: {exc}", cause=exc,
        ) from exc
    if not isinstance(data, dict):
        return None
    value = data.get("backend")
    if value is None:
        return None
    if value == "local":
        return "local"
    if value == "cloud":
        return "cloud"
    raise ControlPlaneError(
        f"{path}: 'backend' must be 'local' or 'cloud', got {value!r}",
    )


def _validate_backend_name(value: str, source: str) -> BackendName:
    """Narrow `str` to `BackendName` or raise with the source's name."""
    if value == "local":
        return "local"
    if value == "cloud":
        return "cloud"
    raise ControlPlaneError(
        f"{source} must be 'local' or 'cloud', got {value!r}",
    )


def resolve_backend(flag_value: str | None) -> BackendName:
    """Resolve which backend the active command should target.

    Order: explicit flag > env var > config file > auto-fallback. Raises
    `ControlPlaneError` when the chosen backend cannot be honored (e.g.
    `cloud` requested but `ORCA_CLOUD_URL` / `ORCA_CLOUD_API_KEY` missing).
    """
    if flag_value:
        return _validate_backend_name(flag_value, "--backend")
    env_value = os.environ.get(_ENV_BACKEND)
    if env_value:
        return _validate_backend_name(env_value, _ENV_BACKEND)
    config_value = _read_config_backend()
    if config_value is not None:
        return config_value
    return _autodetect_backend()


def _autodetect_backend() -> BackendName:
    """Pick a backend when no explicit signal was provided.

    Probes the loopback daemon first (since standalone users dominate); if it
    answers, choose `local`. If not, but cloud creds are present, choose
    `cloud`. Otherwise fail clear.
    """
    if _has_cloud_creds() and not _daemon_reachable():
        return "cloud"
    if _daemon_reachable():
        return "local"
    raise BackendNotResolvedError(
        "no backend resolved: no daemon is running on 127.0.0.1. Run 'orca start' first. "
        f"To target a cloud deployment instead, set {_ENV_CLOUD_URL} and {_ENV_CLOUD_API_KEY}.",
    )


@functools.cache
def cloud_backend_installed() -> bool:
    """True when a package implements the hosted-only verbs. The public build has none."""
    return any(True for _ in entry_points(group=_CLI_BACKENDS_GROUP, name="cloud"))


def cloud_help(text: str) -> str:
    """`text` when the hosted-only verbs are installed, else nothing: help never offers what the build lacks."""
    return text if cloud_backend_installed() else ""


def _has_cloud_creds() -> bool:
    return bool(os.environ.get(_ENV_CLOUD_URL)) and bool(
        os.environ.get(_ENV_CLOUD_API_KEY),
    )


def _daemon_reachable() -> bool:
    """True iff the loopback daemon is recorded AND answers GET /health.

    Silent probe -- never prints to stderr, so auto-detect can fall through to
    the combined "no backend resolved" report.
    """
    # Use pid_file_path() (function) NOT the frozen PID_FILE_PATH constant
    # so tests that set ORCA_DAEMON_HOME post-import see their override.
    info = detect_live_daemon(pid_file_path())
    if info is None:
        return False
    return OrcaClient(f"http://127.0.0.1:{info.port}", timeout=2.0).health()


def _resolve_cloud_backend_factory() -> type[ICloudControlPlaneClient]:
    """The class registered for the ``orca.cli_backends`` entry-point group, name ``cloud``."""
    for ep in entry_points(group=_CLI_BACKENDS_GROUP, name="cloud"):
        return ep.load()
    output.fail(
        _NO_CLOUD_BACKEND,
        code=output.EXIT_USAGE,
    )


def _local_daemon_url() -> str:
    """The loopback URL from the PID file."""
    info = detect_live_daemon(pid_file_path())
    if info is None:
        raise ControlPlaneError(
            "no daemon running (run `orca start` first)",
            exit_code=output.EXIT_NOT_CONNECTED,
        )
    return f"http://127.0.0.1:{info.port}"


def make_client(backend: BackendName) -> IControlPlaneClient:
    """The one client, pointed at the resolved server. No silent fallback; missing creds raises."""
    if backend == "local":
        return OrcaClient(_local_daemon_url())
    base_url = os.environ.get(_ENV_CLOUD_URL, "")
    api_key = os.environ.get(_ENV_CLOUD_API_KEY, "")
    if not base_url or not api_key:
        raise ControlPlaneError(
            f"backend=cloud requires {_ENV_CLOUD_URL} and {_ENV_CLOUD_API_KEY} "
            "to be set in the environment",
            exit_code=output.EXIT_USAGE,
        )
    return OrcaClient(base_url, api_key)


def _resolve_or_fail() -> BackendName:
    """Resolve the backend from CLI state; map errors to typed exits.

    `BackendNotResolvedError` (no daemon AND no cloud creds AND no explicit
    signal) maps to EXIT_NOT_CONNECTED so scripts can distinguish it from
    EXIT_USAGE (bad --backend value, missing creds after explicit cloud).
    """
    from orca.cli.app import STATE
    try:
        return resolve_backend(STATE.backend)
    except BackendNotResolvedError as exc:
        output.fail(str(exc), code=output.EXIT_NOT_CONNECTED)
    except ControlPlaneError as exc:
        output.fail(str(exc), code=output.EXIT_USAGE)


def get_client() -> IControlPlaneClient:
    """The client for the active server, from the state the root callback set."""
    return make_client(_resolve_or_fail())


def require_local() -> None:
    """Exit non-zero if the active backend is not `local` (fail-clean)."""
    backend = _resolve_or_fail()
    if backend != "local":
        output.fail(_LOCAL_REDIRECT, code=output.EXIT_USAGE)


def daemon_client() -> DaemonLoadingClient:
    """The client for the daemon-only verbs. Exits if the active server is not the daemon."""
    require_local()
    return DaemonLoadingClient(_local_daemon_url())


def require_cloud() -> None:
    """Exit non-zero if the active backend is not `cloud` (fail-clean).

    With no cloud backend installed, say that instead of redirecting to a
    `--backend cloud` that cannot work.
    """
    if not cloud_backend_installed():
        output.fail(_NO_CLOUD_BACKEND, code=output.EXIT_USAGE)
    backend = _resolve_or_fail()
    if backend != "cloud":
        output.fail(_CLOUD_REDIRECT, code=output.EXIT_USAGE)


def cloud_client() -> ICloudControlPlaneClient:
    """The client for the hosted-only verbs.

    Resolves the backend before reading the credentials: a build that ships no
    implementation has nothing the credentials would reach.
    """
    require_cloud()
    cloud_cls = _resolve_cloud_backend_factory()
    base_url = os.environ.get(_ENV_CLOUD_URL, "")
    api_key = os.environ.get(_ENV_CLOUD_API_KEY, "")
    if not base_url or not api_key:
        output.fail(
            f"backend=cloud requires {_ENV_CLOUD_URL} and {_ENV_CLOUD_API_KEY} "
            "to be set in the environment",
            code=output.EXIT_USAGE,
        )
    return cloud_cls(base_url=base_url, api_key=api_key)


def active_backend() -> BackendName:
    """Resolved backend name for verbs that branch on it (e.g. `device list`)."""
    return _resolve_or_fail()
