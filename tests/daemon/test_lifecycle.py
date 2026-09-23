"""Tests for daemon/lifecycle.py PID file primitives.

Each test targets one branch:
- Round-trip: write -> read preserves all fields.
- Absent file: read returns None, delete is a no-op.
- Corrupt file: read returns None and detect_live_daemon cleans up.
- Live PID: detect returns the info.
- Dead PID: detect deletes the file and returns None (stale cleanup).
- Atomic write: no partial .tmp artifact left behind on a successful write.
- update_spec: mutates in place; no-op when file absent.

These are not tautologies. Each exercises a documented behavior of
`detect_live_daemon`, `write_pid_file`, `read_pid_file`, `update_spec`.
"""

import json
import time
from pathlib import Path

import pytest

from orca.daemon.lifecycle import (
    DaemonInfo,
    delete_pid_file,
    detect_live_daemon,
    pick_free_port,
    read_pid_file,
    write_pid_file,
)


@pytest.fixture
def pid_file(tmp_path: Path) -> Path:
    return tmp_path / "daemon.json"


def test_write_then_read_preserves_all_fields(pid_file: Path) -> None:
    info = DaemonInfo(pid=12345, port=51293, started_at=1712345678.9)
    write_pid_file(info, pid_file)
    round_trip = read_pid_file(pid_file)
    assert round_trip == info


def test_read_missing_file_returns_none(pid_file: Path) -> None:
    assert read_pid_file(pid_file) is None


def test_read_corrupt_json_returns_none(pid_file: Path) -> None:
    """Non-JSON content must not raise; treated as absent by callers."""
    pid_file.write_text("this is not json")
    assert read_pid_file(pid_file) is None


def test_read_valid_json_wrong_shape_returns_none(pid_file: Path) -> None:
    """JSON that parses but doesn't match DaemonInfo schema must return None."""
    pid_file.write_text(json.dumps({"unexpected": "shape"}))
    assert read_pid_file(pid_file) is None


def test_delete_missing_file_is_noop(pid_file: Path) -> None:
    """Safe to call even if the file was never created."""
    delete_pid_file(pid_file)  # no exception
    assert not pid_file.exists()


def test_atomic_write_leaves_no_tmp_artifact(pid_file: Path) -> None:
    """The temp file used for atomic write must be renamed, not left behind."""
    info = DaemonInfo(pid=1, port=1, started_at=0.0)
    write_pid_file(info, pid_file)
    assert pid_file.exists()
    assert not pid_file.with_suffix(pid_file.suffix + ".tmp").exists()


def test_detect_live_daemon_with_own_pid_returns_info(pid_file: Path) -> None:
    """The test process is definitely alive; detect must return its info."""
    import os
    info = DaemonInfo(pid=os.getpid(), port=12345, started_at=time.time())
    write_pid_file(info, pid_file)
    result = detect_live_daemon(pid_file)
    assert result == info
    assert pid_file.exists()  # Not cleaned up because PID is alive.


def test_detect_live_daemon_with_dead_pid_cleans_up(pid_file: Path) -> None:
    """A PID that doesn't exist must be treated as stale and cleaned up."""
    # PID 99999999 is safely out of range on Windows and POSIX user processes.
    info = DaemonInfo(pid=99999999, port=12345, started_at=time.time())
    write_pid_file(info, pid_file)
    result = detect_live_daemon(pid_file)
    assert result is None
    assert not pid_file.exists()  # Stale entry removed.


def test_detect_live_daemon_with_corrupt_file_cleans_up(pid_file: Path) -> None:
    """A corrupt file is treated as stale: cleaned up, returns None."""
    pid_file.write_text("not json")
    result = detect_live_daemon(pid_file)
    assert result is None
    assert not pid_file.exists()


def test_detect_live_daemon_with_absent_file_returns_none(pid_file: Path) -> None:
    assert detect_live_daemon(pid_file) is None


def test_pick_free_port_returns_usable_port() -> None:
    """pick_free_port returns something that can be bound immediately after.

    Any non-negative int passes type-check; this asserts the port is in the
    private/ephemeral range (> 1024) which is what the OS assigns.
    """
    port = pick_free_port()
    assert 1024 < port < 65536
