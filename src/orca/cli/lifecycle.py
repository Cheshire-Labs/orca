"""CLI lifecycle verbs: start / shutdown / unload.

- `orca start`     spawns the daemon as a detached subprocess, polls /health,
                   prints the PID and port. Does NOT load a system; the
                   daemon is idle until `orca topology mount`.
- `orca unload`    POST /unload. Tears down the loaded SystemRuntime; daemon
                   keeps running, ready for another `orca topology mount`.
- `orca shutdown`  POST /shutdown. Daemon unloads (if needed) then exits.

Topology mount + workflow load are backend-dispatched verbs that live in
`orca.cli.topology` / `orca.cli.registry`, not here.

All HTTP calls dial http://127.0.0.1:<port> where port is read from the
PID file.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import typer

from orca.cli import output
from orca.daemon.lifecycle import (
    DaemonInfo,
    delete_pid_file,
    detect_live_daemon,
    log_file_path,
    pick_free_port,
    pid_file_path as _pid_file_path,
    read_pid_file,
)
from orca.daemon.schemas import (
    ErrorResponse,
    ShutdownResponse,
    UnloadResponse,
)


_HEALTH_POLL_INTERVAL_S = 0.1
# A first start on a fresh install compiles every module it imports: 18 s on a laptop.
_HEALTH_POLL_TIMEOUT_S = 60.0
# The runtime's shutdown bounds each of its four drains at 30 s, and draining
# live executions comes before them. /shutdown runs the same drains as /unload.
_UNLOAD_TIMEOUT_S = 180.0
# How long a signalled daemon gets to remove its own PID file.
_SIGNAL_GRACE_S = 5.0


def _base_url(info: DaemonInfo) -> str:
    return f"http://127.0.0.1:{info.port}"


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
    """Poll GET http://127.0.0.1:port/health until 200 or `_HEALTH_POLL_TIMEOUT_S` runs out.

    Read at call time, so the wait and the failure message that names it always agree.
    """
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + _HEALTH_POLL_TIMEOUT_S
    with httpx.Client(timeout=1.0) as client:
        while time.time() < deadline:
            try:
                r = client.get(url)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
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

    Run it from your project root: the daemon imports the modules that
    `orca topology mount` and `orca workflow load` name from this directory.
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
        "No system loaded -- run `orca topology mount <spec>` next.",
    )


def shutdown() -> None:
    """Stop the daemon. Unloads the loaded system first if present."""
    info = detect_live_daemon(_pid_file_path())
    if info is None:
        output.info("no daemon running")
        return

    url = f"{_base_url(info)}/shutdown"
    try:
        with httpx.Client(timeout=_UNLOAD_TIMEOUT_S) as client:
            r = client.post(url)
            if r.status_code == 200:
                ShutdownResponse.model_validate(r.json())
                output.info(f"daemon stopped (pid={info.pid})")
                # The daemon removes its own PID file on exit.
                return
            # Unexpected status from a live-looking daemon -- fall through
            # to SIGTERM fallback.
    except httpx.HTTPError:
        pass

    # Fallback: daemon didn't acknowledge shutdown; signal it directly.
    output.warn(
        f"daemon did not acknowledge /shutdown; sending signal (pid={info.pid})",
    )
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


def unload() -> None:
    """Unload the current topology. Daemon keeps running."""
    info = _require_running_daemon()
    url = f"{_base_url(info)}/unload"
    try:
        with httpx.Client(timeout=_UNLOAD_TIMEOUT_S) as client:
            r = client.post(url)
    except httpx.HTTPError as e:
        message, code = output.transport_failure(
            e, f"the daemon at {_base_url(info)}", _UNLOAD_TIMEOUT_S,
        )
        output.fail(message, code=code)
    if r.status_code != 200:
        err = _parse_error(r)
        output.fail(
            f"/unload failed ({r.status_code}): {err}",
            code=output.exit_code_for_status(r.status_code),
        )
    UnloadResponse.model_validate(r.json())
    output.info("topology unloaded; daemon still running")


def _parse_error(resp: httpx.Response) -> str:
    try:
        return ErrorResponse.model_validate(resp.json()).detail
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"
