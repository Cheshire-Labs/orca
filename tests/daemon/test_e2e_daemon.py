"""End-to-end subprocess test for the daemon lifecycle.

Unlike the other daemon tests, this one actually spawns `python -m orca.daemon`
as a detached child process and talks to it over real HTTP. It is the one
place where cross-platform detachment quirks (Windows DETACHED_PROCESS etc.)
are exercised in CI.

What it catches:
- Detachment is broken (child process dies with the parent) -- would manifest
  as /health never responding or the process exiting before we can dial it.
- The daemon does not actually write its PID file on startup.
- POST /shutdown doesn't actually exit the subprocess (test times out
  waiting for the child to finish).
- PID file isn't cleaned up on orderly exit.

Uses `ORCA_DAEMON_HOME` to isolate into tmp_path so a real user-level
daemon (if any) is not disturbed.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

from orca.daemon.lifecycle import DaemonInfo, read_pid_file


_STARTUP_TIMEOUT_S = 45.0
_SHUTDOWN_TIMEOUT_S = 10.0


def _spawn_daemon_isolated(
    port: int, pid_dir: Path,
) -> subprocess.Popen[bytes]:
    env = {**os.environ, "ORCA_DAEMON_HOME": str(pid_dir)}
    cmd = [sys.executable, "-m", "orca.daemon", "--port", str(port)]
    if sys.platform == "win32":
        # Must match cli/lifecycle.py's _spawn_daemon: CREATE_NO_WINDOW hides
        # the console window; CREATE_NEW_PROCESS_GROUP isolates signal
        # delivery.
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
            env=env,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    return proc


def _pick_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _daemon_is_gone(proc: subprocess.Popen[bytes]) -> bool:
    """True once the spawned process is known not to be serving.

    POSIX detaches by forking and having the tracked child ``os._exit(0)``
    (``orca/daemon/__main__.py``), so a ZERO exit there is the HEALTHY path
    and says nothing -- only a non-zero exit means gone. Windows does not
    fork: the launcher waits on the real Python and forwards its code, so any
    exit at all means the daemon is not coming up.
    """
    code = proc.poll()
    if code is None:
        return False
    return True if sys.platform == "win32" else code != 0


def _wait_for_health(
    port: int, timeout_s: float, proc: subprocess.Popen[bytes],
) -> bool:
    """Poll /health until it answers, the daemon dies, or the budget runs out.

    Watching ``proc`` turns a dead daemon into an immediate answer instead of
    a full-budget wait: the spawn either produces a serving process or it
    does not, and waiting out the clock on a corpse only hides the exit code.
    """
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    with httpx.Client(timeout=1.0) as client:
        while time.time() < deadline:
            try:
                if client.get(url).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            if _daemon_is_gone(proc):
                return False
            time.sleep(0.1)
    return False


def _wait_for_exit(pid: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return False


def _force_kill(pid: int) -> None:
    if psutil.pid_exists(pid):
        try:
            psutil.Process(pid).terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


@pytest.mark.timeout(120)
def test_daemon_start_health_shutdown_roundtrip(tmp_path: Path) -> None:
    """Full lifecycle: spawn -> /health responds -> POST /shutdown -> exits.

    If detachment is broken on Windows, /health would never respond. If
    /shutdown doesn't actually exit, the wait_for_exit assert fails.

    Note: on Windows, sys.executable is a launcher that respawns the real
    Python as a grandchild, so `proc.pid` (the launcher) differs from the
    daemon's own `os.getpid()`. We use the PID from the file -- that's the
    actual daemon process -- for every subsequent check.
    """
    port = _pick_free_port()
    proc = _spawn_daemon_isolated(port, tmp_path)
    daemon_pid: int | None = None
    try:
        assert _wait_for_health(port, _STARTUP_TIMEOUT_S, proc), (
            f"daemon did not come up on /health within {_STARTUP_TIMEOUT_S}s "
            f"(launcher pid={proc.pid}, exit={proc.poll()}, port={port}). "
            f"Inspect {tmp_path}/daemon.log"
        )

        # PID file was written by the daemon itself on startup.
        info = read_pid_file(tmp_path / "daemon.json")
        assert info is not None, "daemon did not write a PID file on startup"
        assert info.port == port
        assert psutil.pid_exists(info.pid), (
            f"daemon.json says pid={info.pid} but no such process"
        )
        daemon_pid = info.pid

        # /health responds with the expected shape.
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"http://127.0.0.1:{port}/health")
            assert r.status_code == 200
            body = r.json()
            assert body["daemon"] == "ok"
            assert body["system_loaded"] is False

            # Shutdown via HTTP; daemon exits gracefully.
            r = client.post(f"http://127.0.0.1:{port}/shutdown")
            assert r.status_code == 200

        assert _wait_for_exit(daemon_pid, _SHUTDOWN_TIMEOUT_S), (
            f"daemon (pid={daemon_pid}) did not exit within "
            f"{_SHUTDOWN_TIMEOUT_S}s after POST /shutdown"
        )
        assert not (tmp_path / "daemon.json").exists(), (
            "daemon exited but did not clean up its PID file"
        )
    finally:
        # Belt-and-suspenders cleanup: kill launcher + daemon process if alive.
        _force_kill(proc.pid)
        if daemon_pid is not None:
            _force_kill(daemon_pid)


def test_detect_live_daemon_finds_running_daemon(tmp_path: Path) -> None:
    """detect_live_daemon returns a DaemonInfo for a real running daemon.

    Proves the PID file's pid + port round-trip through the filesystem and
    are recognized as live via psutil.pid_exists. `orca start` relies on
    this to refuse a second spawn.
    """
    from orca.daemon.lifecycle import detect_live_daemon

    port = _pick_free_port()
    proc = _spawn_daemon_isolated(port, tmp_path)
    daemon_pid: int | None = None
    try:
        assert _wait_for_health(port, _STARTUP_TIMEOUT_S, proc)

        pid_file = tmp_path / "daemon.json"
        info = detect_live_daemon(pid_file)
        assert info is not None
        assert info.port == port
        assert psutil.pid_exists(info.pid)
        daemon_pid = info.pid
        # PID file was NOT cleaned up (process is alive).
        assert pid_file.exists()
    finally:
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(f"http://127.0.0.1:{port}/shutdown")
        except httpx.HTTPError:
            pass
        if daemon_pid is not None:
            _wait_for_exit(daemon_pid, _SHUTDOWN_TIMEOUT_S)
            _force_kill(daemon_pid)
        _force_kill(proc.pid)
