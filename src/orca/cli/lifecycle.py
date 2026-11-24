"""CLI lifecycle verbs: start / shutdown / unload.

- `orca start`     spawns the daemon as a detached subprocess, polls /health,
                   prints the PID and port. Does NOT load a system; the
                   daemon is idle until `orca topology mount`.
- `orca unload`    POST /api/runtime/unload. Tears down the loaded SystemRuntime; daemon
                   keeps running, ready for another `orca topology mount`.
- `orca shutdown`  POST /shutdown. Daemon unloads (if needed) then exits.

`orca topology mount` is a backend-dispatched verb in `orca.cli.topology`, not here.

All HTTP calls dial http://127.0.0.1:<port> where port is read from the
PID file.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import typer

from orca.cli import output
from orca.cli.client import OrcaClient
from orca.cli.control_plane import ControlPlaneError
from orca.operations.system_loading_models import UNLOAD_ROUTE
from orca.daemon.lifecycle import (
    DaemonInfo,
    delete_pid_file,
    detect_live_daemon,
    is_pid_alive,
    log_file_path,
    pick_free_port,
    pid_file_path as _pid_file_path,
    read_pid_file,
)


_HEALTH_POLL_INTERVAL_S = 0.1
# A first start on a fresh install compiles every module it imports: 18 s on a laptop.
_HEALTH_POLL_TIMEOUT_S = 60.0
# `orca shutdown` stops waiting for /shutdown after this, then signals the daemon.
_SHUTDOWN_DEADLINE_S = 180.0
# An acknowledged daemon still has to disconnect its devices and exit; `orca shutdown` waits this long for that.
_EXIT_DEADLINE_S = 180.0
_EXIT_POLL_INTERVAL_S = 0.1
# How long a signalled daemon gets to remove its own PID file.
_SIGNAL_GRACE_S = 5.0


def _base_url(info: DaemonInfo) -> str:
    return f"http://127.0.0.1:{info.port}"


def _daemon_client(info: DaemonInfo) -> OrcaClient:
    return OrcaClient(_base_url(info), peer=f"the daemon at {_base_url(info)}")


def _require_running_daemon() -> DaemonInfo:
    info = detect_live_daemon(_pid_file_path())
    if info is None:
        output.fail(
            "no daemon running (run `orca start` first)",
            code=output.EXIT_NOT_CONNECTED,
        )
    return info


def _startup_err_path() -> Path:
    """File the spawned daemon's stderr is captured to during startup.

    Used only when the daemon crashes before its own logging setup has a
    chance to write ``daemon.log``. The CLI reads the tail of this file
    and surfaces it to the user on health-check timeout.
    """
    return log_file_path().parent / "daemon_startup.err"


def _spawn_daemon(port: int) -> int:
    """Spawn `python -m orca.daemon --port <port>` as a detached subprocess.

    stderr is captured to ``~/.orca/daemon_startup.err`` so that an import
    error or other pre-logging failure is visible to the CLI parent. A clean
    startup only writes a few lines of uvicorn banner which is harmless; if
    the daemon dies, the traceback is there for ``_read_startup_err`` to
    surface.

    Detachment flags differ by platform; on both, the child survives after
    this CLI process exits.
    """
    cmd = [sys.executable, "-m", "orca.daemon", "--port", str(port)]

    err_path = _startup_err_path()
    err_path.parent.mkdir(parents=True, exist_ok=True)
    err_handle = open(err_path, "w", buffering=1)

    if sys.platform == "win32":
        # CREATE_NO_WINDOW: no console window appears for the child, which
        # matters when operators run `orca start` from a GUI launcher or
        # another terminal -- we don't want to flash a python window at them.
        # CREATE_NEW_PROCESS_GROUP: isolate signal delivery so Ctrl-C in the
        # parent shell doesn't terminate the daemon.
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=err_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )
    else:
        # POSIX: start_new_session detaches from the controlling terminal.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=err_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    err_handle.close()
    return proc.pid


def _read_startup_err(max_lines: int = 40) -> str:
    """Return the tail of the daemon startup-capture file, or empty string.

    Reads only the last few lines so a giant import-time log doesn't flood
    the CLI output. Missing or unreadable file returns empty string; the
    caller falls back to the generic "check daemon.log" hint.
    """
    path = _startup_err_path()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def _wait_for_health(port: int) -> bool:
    """Poll the daemon's `/health` until it answers or `_HEALTH_POLL_TIMEOUT_S` runs out.

    Read at call time, so the wait and the failure message that names it always agree.
    """
    client = OrcaClient(f"http://127.0.0.1:{port}", timeout=1.0)
    deadline = time.time() + _HEALTH_POLL_TIMEOUT_S
    while time.time() < deadline:
        if client.health():
            return True
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    return False


def _stop_spawned_daemon(pid: int) -> None:
    """Stop a daemon that missed the health wait, so a failed `orca start` leaves nothing running."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # Already exited, usually on the import error the startup capture shows.


def start(
    port: int = typer.Option(
        0, "--port",
        help="Bind port. 0 means pick a free ephemeral port automatically.",
    ),
) -> None:
    """Spawn the daemon process. No system loaded; run `orca topology mount` next.

    Run it from your package folder, or set `ORCA_PACKAGE_DIR`: the daemon
    builds that folder's `topology.py` and `workflows/` on `orca topology mount`.
    """
    existing = detect_live_daemon(_pid_file_path())
    if existing is not None:
        output.fail(
            f"daemon already running (pid={existing.pid}, port={existing.port})",
            code=output.EXIT_CONFLICT,
        )

    chosen_port = port if port > 0 else pick_free_port()
    child_pid = _spawn_daemon(chosen_port)
    if not _wait_for_health(chosen_port):
        _stop_spawned_daemon(child_pid)
        # Health never came up. Surface whatever the daemon wrote to its
        # startup capture file so users see import errors / dependency
        # failures without having to re-run the daemon in the foreground.
        captured = _read_startup_err()
        err_path = _startup_err_path()
        hint = (
            f"\n--- tail of {err_path} ---\n{captured}"
            if captured
            else f"\ncheck {err_path} or ~/.orca/daemon.log for details"
        )
        output.fail(
            f"daemon failed to start within {_HEALTH_POLL_TIMEOUT_S:.0f}s and was stopped "
            f"(child pid={child_pid}, port={chosen_port}){hint}",
            code=output.EXIT_TIMEOUT,
        )
    # The daemon wrote its own PID file on startup; re-read for the info.
    info = read_pid_file(_pid_file_path())
    if info is None:
        output.fail(
            "daemon is responding on /health but wrote no PID file; "
            "check ~/.orca/daemon.log",
            code=output.EXIT_GENERIC,
        )
    output.info(
        f"daemon started (pid={info.pid}, port={info.port}). "
        "No system loaded -- run `orca topology mount` next.",
    )


def shutdown() -> None:
    """Stop the daemon. Unloads the loaded system first if present."""
    info = detect_live_daemon(_pid_file_path())
    if info is None:
        output.info("no daemon running")
        return

    acknowledged = _shutdown_acknowledged(info)
    # The answer comes before the process exits, and a live PID file makes the next `orca start` refuse.
    if acknowledged and _exited(info.pid):
        output.info(f"daemon stopped (pid={info.pid})")
        # The daemon removes its own PID file on exit.
        return

    happened = "acknowledged /shutdown and is still running" if acknowledged else "did not acknowledge /shutdown"
    output.warn(f"daemon {happened}; sending signal (pid={info.pid})")
    try:
        os.kill(info.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        output.warn(f"could not signal daemon: {e}")
    # Give the daemon a moment to self-clean, then force-remove the PID file
    # if it's still present.
    time.sleep(_SIGNAL_GRACE_S)
    if Path(_pid_file_path()).exists():
        delete_pid_file(_pid_file_path())
    output.info("daemon stopped (forced)")


def _exited(pid: int) -> bool:
    """Whether the daemon's process ended within `_EXIT_DEADLINE_S`."""
    deadline = time.time() + _EXIT_DEADLINE_S
    while is_pid_alive(pid):
        if time.time() >= deadline:
            return False
        time.sleep(_EXIT_POLL_INTERVAL_S)
    return True


def _shutdown_acknowledged(info: DaemonInfo) -> bool:
    """Whether the daemon acknowledged /shutdown within `_SHUTDOWN_DEADLINE_S`."""
    finished = threading.Event()
    acknowledged = threading.Event()

    def ask() -> None:
        try:
            _daemon_client(info).shutdown()
            acknowledged.set()
        except ControlPlaneError:
            pass
        finally:
            finished.set()

    # A daemon thread: one left blocked on a hung daemon must not keep the CLI from exiting.
    threading.Thread(target=ask, daemon=True).start()
    finished.wait(_SHUTDOWN_DEADLINE_S)
    return acknowledged.is_set()


def unload() -> None:
    """Unload the current topology. Daemon keeps running."""
    info = _require_running_daemon()
    try:
        _daemon_client(info).unload()
    except ControlPlaneError as e:
        if e.http_status is None:
            output.fail(str(e), code=e.exit_code or output.EXIT_GENERIC)
        output.fail(
            f"{UNLOAD_ROUTE.binding.url} failed ({e.http_status}): {e.detail}",
            code=output.exit_code_for_status(e.http_status),
        )
    output.info("topology unloaded; daemon still running")
