"""`orca shutdown` returns only after the daemon's process has exited, so `orca start` can follow it at once."""

from pathlib import Path

import pytest

from orca.cli import lifecycle
from orca.daemon.lifecycle import DaemonInfo

_DAEMON = DaemonInfo(pid=4242, port=1, started_at=0.0)


class _Acknowledges:
    def shutdown(self) -> None:
        return None


def test_shutdown_waits_for_a_daemon_that_acknowledged_and_is_still_exiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    alive_answers = [True, True, False]
    asked: list[int] = []

    def is_pid_alive(pid: int) -> bool:
        asked.append(pid)
        return alive_answers.pop(0)

    monkeypatch.setattr(lifecycle, "detect_live_daemon", lambda _: _DAEMON)
    monkeypatch.setattr(lifecycle, "_pid_file_path", lambda: tmp_path / "daemon.json")
    monkeypatch.setattr(lifecycle, "_daemon_client", lambda _: _Acknowledges())
    monkeypatch.setattr(lifecycle, "is_pid_alive", is_pid_alive)
    monkeypatch.setattr(lifecycle, "_EXIT_POLL_INTERVAL_S", 0.0)

    lifecycle.shutdown()

    assert asked == [_DAEMON.pid] * 3
    assert "daemon stopped" in capsys.readouterr().err
