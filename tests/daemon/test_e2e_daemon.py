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

import time
from pathlib import Path

import httpx
import psutil
import pytest

from orca.daemon.lifecycle import read_pid_file
from orca.runtime.store_factory import default_package_root
from tests.isolated_daemon import START_LIMIT_S, IsolatedDaemon, pick_free_port, start_isolated_daemon


_SHUTDOWN_TIMEOUT_S = 10.0
_TEST_LIMIT_S = START_LIMIT_S + 120.0


def _started_on(home: Path, picked: list[int]) -> IsolatedDaemon:
    """A daemon started the one way tests start one, noting each port it was told to take."""
    def pick() -> int:
        picked.append(pick_free_port())
        return picked[-1]

    return start_isolated_daemon(home, default_package_root(), pick_port=pick)


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


@pytest.mark.timeout(_TEST_LIMIT_S)
def test_daemon_start_health_shutdown_roundtrip(tmp_path: Path) -> None:
    """Full lifecycle: spawn -> /health responds -> POST /shutdown -> exits.

    If detachment is broken on Windows, /health would never respond. If
    /shutdown doesn't actually exit, the wait_for_exit assert fails.

    Note: on Windows, sys.executable is a launcher that respawns the real
    Python as a grandchild, so the launcher's pid differs from the
    daemon's own `os.getpid()`. We use the PID from the file -- that's the
    actual daemon process -- for every subsequent check.
    """
    picked: list[int] = []
    daemon = _started_on(tmp_path, picked)
    port = picked[-1]
    daemon_pid: int | None = None
    try:
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
            assert body["status"] == "ok"
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
        _force_kill(daemon.process.pid)
        if daemon_pid is not None:
            _force_kill(daemon_pid)


@pytest.mark.timeout(_TEST_LIMIT_S)
def test_detect_live_daemon_finds_running_daemon(tmp_path: Path) -> None:
    """detect_live_daemon returns a DaemonInfo for a real running daemon.

    Proves the PID file's pid + port round-trip through the filesystem and
    are recognized as live via psutil.pid_exists. `orca start` relies on
    this to refuse a second spawn.
    """
    from orca.daemon.lifecycle import detect_live_daemon

    picked: list[int] = []
    daemon = _started_on(tmp_path, picked)
    port = picked[-1]
    daemon_pid: int | None = None
    try:
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
        _force_kill(daemon.process.pid)
