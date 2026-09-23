"""PID/port file management for the orca daemon.

One running daemon per user. State lives at `~/.orca/daemon.json`:

    {"pid": 12345, "port": 51293, "started_at": 1712345678.9, "spec": "..."}

`orca start` writes this file; `orca shutdown` removes it; `orca topology mount`/
`unload` update the `spec` field. Stale entries (daemon died without cleanup) are
detected via psutil.pid_exists and removed on the next `orca start`.

psutil is used for cross-platform liveness (Windows + POSIX in one call).
"""

import json
import os
from pathlib import Path
from typing import cast

import psutil
from pydantic import BaseModel


def _daemon_home() -> Path:
    """Directory holding daemon.json and daemon.log.

    `ORCA_DAEMON_HOME` env var overrides the default ~/.orca. Tests set it
    to isolate from any real daemon the user may have running; subprocesses
    the tests spawn inherit the override so their self-written PID file
    also lands in the tmp dir.
    """
    override = os.environ.get("ORCA_DAEMON_HOME")
    return Path(override) if override else Path.home() / ".orca"


def pid_file_path() -> Path:
    """Current PID file path, evaluated against live env. Prefer this in
    code paths that may be called after tests set `ORCA_DAEMON_HOME`
    post-import -- the module-level `PID_FILE_PATH` constant froze at
    import time and would not pick up the override."""
    return _daemon_home() / "daemon.json"


def log_file_path() -> Path:
    return _daemon_home() / "daemon.log"


# Kept as module-level constants for callers (and tests) that want the
# "import-time default" explicitly. Prefer the functions above when the
# live env value matters.
PID_FILE_PATH = _daemon_home() / "daemon.json"
LOG_FILE_PATH = _daemon_home() / "daemon.log"


class DaemonInfo(BaseModel):
    """Contents of ~/.orca/daemon.json.

    Intentionally minimal: what a CLI client needs to dial the daemon
    (port) and verify it's really running (pid for liveness check, plus
    started_at for display). The loaded spec lives in in-memory daemon
    state and is surfaced via GET /health -- no need to duplicate it on
    disk where it could drift.
    """
    pid: int
    port: int
    started_at: float


def is_pid_alive(pid: int) -> bool:
    """True if a process with this PID is running.

    psutil.pid_exists is cross-platform (Windows + POSIX). Treats PID 0 and
    negative PIDs as not alive defensively, though psutil handles them.
    """
    if pid <= 0:
        return False
    return psutil.pid_exists(pid)


def read_pid_file(path: Path = PID_FILE_PATH) -> DaemonInfo | None:
    """Read the PID file. Returns None if absent, corrupt, or malformed.

    Malformed files are treated the same as absence: the caller will either
    delete (stale) or replace them. Never raises on parse failure -- the
    file is a best-effort coordination artifact, not a durable contract.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return DaemonInfo.model_validate(raw)
    except Exception:  # pydantic ValidationError: treat as corrupt
        return None


def write_pid_file(info: DaemonInfo, path: Path = PID_FILE_PATH) -> None:
    """Atomically write the PID file (temp + rename).

    Parent dir is created if missing. The temp-rename guarantees readers
    never see a partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(info.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def delete_pid_file(path: Path = PID_FILE_PATH) -> None:
    """Remove the PID file. No-op if already absent."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def detect_live_daemon(path: Path = PID_FILE_PATH) -> DaemonInfo | None:
    """Return the DaemonInfo iff a live daemon is recorded.

    - File absent -> None (no daemon).
    - File present, PID dead -> deletes the file and returns None (stale).
    - File present, PID alive -> returns the DaemonInfo.
    - File corrupt -> deletes the file and returns None (treated as stale).
    """
    if not path.exists():
        return None
    info = read_pid_file(path)
    if info is None:
        # Corrupt file. Clean up so the next start doesn't trip over it.
        delete_pid_file(path)
        return None
    if not is_pid_alive(info.pid):
        delete_pid_file(path)
        return None
    return info


def pick_free_port(host: str = "127.0.0.1") -> int:
    """Bind to port 0, read the OS-assigned port, release the socket.

    There's an inherent TOCTOU between the release and the subsequent bind
    by the daemon, but this is standard practice and the window is small.
    Cross-platform.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return cast(int, s.getsockname()[1])
