"""Start a daemon subprocess for a test, and stop it, without touching another test's daemon."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil

from orca.daemon.lifecycle import DaemonInfo, read_pid_file
from orca.operations.wire_models import HealthResponse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Bounds a start that hangs; the wait returns at the daemon's first answer. A start takes about 15 s alone and
# passed 45 s with four test workers on one machine.
START_LIMIT_S = 180.0
_LOG_TAIL_LINES = 30


@dataclass
class IsolatedDaemon:
    process: subprocess.Popen[bytes]
    info: DaemonInfo
    package_dir: Path


class DaemonDidNotStartError(RuntimeError):
    pass


class _PortTaken(Exception):
    pass


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_isolated_daemon(
    home: Path,
    package_dir: Path,
    pick_port: Callable[[], int] = pick_free_port,
    attempts: int = 5,
) -> IsolatedDaemon:
    """A daemon proven to be this test's own: it answers on its port with this test's package folder.

    The picked port is free only until the daemon binds it, so another test's daemon or client can take it first.
    Then this daemon exits or another one answers; either way the attempt is dropped and a new port is tried.
    """
    for _ in range(attempts):
        # A daemon killed while starting leaves its PID file behind; this attempt must read only its own.
        (home / "daemon.json").unlink(missing_ok=True)
        process = _spawn(pick_port(), home, package_dir)
        try:
            return IsolatedDaemon(process, _wait_for_own_daemon(process, home, package_dir), package_dir)
        except _PortTaken:
            continue
        except BaseException:
            _kill(process, read_pid_file(home / "daemon.json"))
            raise
    raise DaemonDidNotStartError(f"every port tried was taken ({attempts} attempts, see {home}/daemon.log)")


def stop_isolated_daemon(daemon: IsolatedDaemon) -> None:
    if _is_own(daemon.info.port, daemon.package_dir):
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(f"http://127.0.0.1:{daemon.info.port}/shutdown")
        except httpx.HTTPError:
            pass
        try:
            psutil.Process(daemon.info.pid).wait(timeout=10.0)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass
    _kill(daemon.process, daemon.info)


def _spawn(port: int, home: Path, package_dir: Path) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "ORCA_DAEMON_HOME": str(home),
        "ORCA_PACKAGE_DIR": str(package_dir),
        "PYTHONPATH": str(_PROJECT_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    cmd = [sys.executable, "-m", "orca.daemon", "--port", str(port)]
    if sys.platform == "win32":
        return subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True, env=env,
        )
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True, env=env,
    )


def _wait_for_own_daemon(process: subprocess.Popen[bytes], home: Path, package_dir: Path) -> DaemonInfo:
    """The daemon writes its PID file before it binds, and removes it when it exits, so the file tracks it.

    On POSIX the daemon forks and the spawned process exits at once, so only the PID file names the daemon.
    """
    deadline = time.time() + START_LIMIT_S
    seen: DaemonInfo | None = None
    while time.time() < deadline:
        info = read_pid_file(home / "daemon.json")
        if info is not None:
            seen = info
            if not psutil.pid_exists(info.pid):
                _kill(process, info)
                raise _PortTaken()
            answer = _health(info.port)
            if answer is not None:
                if Path(answer.package_dir).resolve() == package_dir.resolve():
                    return info
                _kill(process, info)
                raise _PortTaken()
        elif seen is not None:
            # It wrote its PID file, then removed it on the way out: the bind failed.
            _kill(process, seen)
            raise _PortTaken()
        elif process.poll() not in (None, 0):
            raise DaemonDidNotStartError(f"the daemon exited with {process.returncode}. {daemon_log_tail(home)}")
        time.sleep(0.1)
    raise DaemonDidNotStartError(f"the daemon did not answer within {START_LIMIT_S}s. {daemon_log_tail(home)}")


def daemon_log_tail(home: Path) -> str:
    """The last lines the daemon logged, for a failure message."""
    log = home / "daemon.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"It wrote no {log}."
    if not lines:
        return f"{log} is empty."
    return f"Last lines of {log}:\n" + "\n".join(lines[-_LOG_TAIL_LINES:])


def _health(port: int) -> HealthResponse | None:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return HealthResponse.model_validate_json(response.content)


def _is_own(port: int, package_dir: Path) -> bool:
    answer = _health(port)
    return answer is not None and Path(answer.package_dir).resolve() == package_dir.resolve()


def _kill(process: subprocess.Popen[bytes], info: DaemonInfo | None) -> None:
    # On Windows a venv's python.exe is a launcher with the daemon as its child; on POSIX the daemon is a fork.
    family: list[psutil.Process] = []
    for pid in (process.pid, info.pid if info is not None else None):
        if pid is None:
            continue
        try:
            member = psutil.Process(pid)
            family += [*member.children(recursive=True), member]
        except psutil.NoSuchProcess:
            pass
    for member in family:
        try:
            member.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(family, timeout=10.0)
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        pass
