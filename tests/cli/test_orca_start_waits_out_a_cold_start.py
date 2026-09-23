"""`orca start` waits out a slow first start, and a start it gives up on leaves no daemon running.

A first start on a fresh install compiles every module it imports; that took 18 s on a
developer laptop. The clock is faked, so these tests wait no real time.
"""

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from orca.cli import lifecycle, output
from orca.cli.app import app
from orca.daemon.lifecycle import DaemonInfo, pid_file_path, write_pid_file

runner = CliRunner()

_COLD_START_S = 18.0


class _Clock:
    """Stands in for the `time` module inside `orca.cli.lifecycle`."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Clock:
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    fake = _Clock()
    monkeypatch.setattr(lifecycle, "time", fake)
    return fake


def _health_answers_after(monkeypatch: pytest.MonkeyPatch, clock: _Clock, seconds: float) -> None:
    real_client = httpx.Client

    def answer(request: httpx.Request) -> httpx.Response:
        if clock.now < seconds:
            raise httpx.ConnectError("daemon still starting", request=request)
        return httpx.Response(200, json={"status": "ok"})

    def client(timeout: float) -> httpx.Client:
        return real_client(timeout=timeout, transport=httpx.MockTransport(answer))

    monkeypatch.setattr(httpx, "Client", client)


@pytest.fixture
def child() -> Iterator[subprocess.Popen[bytes]]:
    """A real process standing in for the spawned daemon."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield proc
    proc.kill()
    proc.wait()


def test_orca_start_waits_out_a_cold_start(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, child: subprocess.Popen[bytes],
) -> None:
    def spawn(port: int) -> int:
        write_pid_file(DaemonInfo(pid=child.pid, port=port, started_at=time.time()), pid_file_path())
        return child.pid

    monkeypatch.setattr(lifecycle, "_spawn_daemon", spawn)
    _health_answers_after(monkeypatch, clock, _COLD_START_S)

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 0, result.output
    assert f"daemon started (pid={child.pid}" in result.output


def test_a_start_that_gives_up_stops_the_daemon_it_spawned(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, child: subprocess.Popen[bytes],
) -> None:
    """Otherwise the daemon comes up after the CLI said it failed, and the next start says 'already running'."""
    monkeypatch.setattr(lifecycle, "_spawn_daemon", lambda port: child.pid)
    _health_answers_after(monkeypatch, clock, float("inf"))

    result = runner.invoke(app, ["start"])

    assert result.exit_code == output.EXIT_TIMEOUT, result.output
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail("orca start gave up but left the daemon it spawned running")
