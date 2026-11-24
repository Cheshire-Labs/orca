"""`orca shutdown` sends SIGTERM to a daemon that does not answer /shutdown within its deadline."""

import signal
import threading
from pathlib import Path

import pytest

from orca.cli import lifecycle
from orca.daemon.lifecycle import DaemonInfo

_DAEMON = DaemonInfo(pid=4242, port=1, started_at=0.0)


class _NeverAnswers:
    def __init__(self) -> None:
        self.release = threading.Event()

    def shutdown(self) -> None:
        self.release.wait(5.0)


def test_a_daemon_that_never_answers_shutdown_is_signalled_after_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    client = _NeverAnswers()
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(lifecycle, "detect_live_daemon", lambda _: _DAEMON)
    monkeypatch.setattr(lifecycle, "_pid_file_path", lambda: tmp_path / "daemon.json")
    monkeypatch.setattr(lifecycle, "_daemon_client", lambda _: client)
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_DEADLINE_S", 0.05)
    monkeypatch.setattr(lifecycle, "_SIGNAL_GRACE_S", 0.0)
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    try:
        lifecycle.shutdown()
    finally:
        client.release.set()

    assert signalled == [(_DAEMON.pid, signal.SIGTERM)]
