"""Tests for cli/client.py OrcaClient and the PID-file lookup. Focuses on my code's branches.

What these catch:
- The clean "no daemon running" failure when the PID file is absent.
  Regression would leave httpx.ConnectError bleeding through to the shell
  with a confusing traceback.
- HTTP status -> CLI exit code mapping (output.exit_code_for_status). Each branch
  is one test; adding a new status code without a mapping would be caught
  only by these tests.
- DTO parsing on success: a 200 response round-trips through the Pydantic
  model, not raw dict. Prevents regressions where someone returns
  `resp.json()` directly to CLI commands.

NOT covered here (would be tautologies):
- "httpx can call httpx" -- the daemon E2E test already does that.
- "the route returns a 200" -- that's test_routes_*.py's job.
"""

import httpx
import pytest

from orca.cli.backend import _local_daemon_url
from orca.cli.client import OrcaClient
from orca.cli.control_plane import ControlPlaneError
from orca.cli import output as cli_output


def test_daemon_client_fails_with_not_connected_when_pid_file_absent(
    tmp_path, monkeypatch,
) -> None:
    """Construction must fail cleanly with the exact exit code shells
    depend on for 'no daemon' detection (10, EXIT_NOT_CONNECTED)."""
    pid_file = tmp_path / "daemon.json"
    assert not pid_file.exists()
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    with pytest.raises(ControlPlaneError) as exc_info:
        _local_daemon_url()
    assert exc_info.value.exit_code == cli_output.EXIT_NOT_CONNECTED


def test_daemon_client_fails_with_not_connected_on_stale_pid_file(
    tmp_path, monkeypatch,
) -> None:
    """A stale PID file (process no longer running) is equivalent to no daemon.

    detect_live_daemon cleans up the stale file AND returns None. Client
    must then raise the same NOT_CONNECTED error. Catches a regression where
    stale detection succeeds but the client proceeds to try to connect.
    """
    from orca.daemon.lifecycle import DaemonInfo, write_pid_file
    pid_file = tmp_path / "daemon.json"
    write_pid_file(
        DaemonInfo(pid=99999999, port=12345, started_at=0.0),
        pid_file,
    )
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    with pytest.raises(ControlPlaneError) as exc_info:
        _local_daemon_url()
    assert exc_info.value.exit_code == cli_output.EXIT_NOT_CONNECTED
    # Stale file got cleaned up by detect_live_daemon.
    assert not pid_file.exists()


def test_exit_code_mapping_covers_expected_statuses() -> None:
    """Each HTTP status -> CLI exit code mapping is my code, not a library.

    If someone reorders the branches in exit_code_for_status and accidentally
    maps 404 -> CONFLICT, the shell's scripts break. Assert each one.
    """
    exit_code_for_status = cli_output.exit_code_for_status

    assert exit_code_for_status(404) == cli_output.EXIT_NOT_FOUND
    assert exit_code_for_status(409) == cli_output.EXIT_CONFLICT
    assert exit_code_for_status(400) == cli_output.EXIT_USAGE
    # Unmapped statuses fall through to EXIT_GENERIC.
    assert exit_code_for_status(500) == cli_output.EXIT_GENERIC
    assert exit_code_for_status(503) == cli_output.EXIT_GENERIC
    assert exit_code_for_status(None) == cli_output.EXIT_GENERIC


def test_daemon_client_maps_404_response_to_not_found_exit(
    tmp_path, monkeypatch,
) -> None:
    """A real 404 from the daemon must surface as EXIT_NOT_FOUND, not
    EXIT_GENERIC or a raw httpx status. Tests the _check() branch."""
    from orca.daemon.lifecycle import DaemonInfo, write_pid_file
    import os
    # Point OrcaClient at this test process (alive) and a fake port that
    # we intercept via httpx.MockTransport.
    pid_file = tmp_path / "daemon.json"
    write_pid_file(
        DaemonInfo(pid=os.getpid(), port=59999, started_at=0.0),
        pid_file,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "execution 'x' not found"})

    # Patch httpx.Client to use a MockTransport so the real network isn't hit.
    orig_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    client = OrcaClient(_local_daemon_url())
    with pytest.raises(ControlPlaneError) as exc_info:
        client.get_execution("whatever-id")
    assert cli_output.exit_code_for_status(exc_info.value.http_status) == cli_output.EXIT_NOT_FOUND


def test_daemon_client_parses_successful_response_into_dto(
    tmp_path, monkeypatch,
) -> None:
    """On 200, the client must return a typed DTO instance, not a raw dict.

    `OrcaClient.submit_workflow` returns `ExecutionRecordModel` (status: str).
    """
    from orca.daemon.lifecycle import DaemonInfo, write_pid_file
    from orca.operations.execution_models import (
    ExecutionRecordModel,
)
    import os

    pid_file = tmp_path / "daemon.json"
    write_pid_file(
        DaemonInfo(pid=os.getpid(), port=59999, started_at=0.0),
        pid_file,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "sub-1", "execution_id": "exec-1", "workflow_name": "wf",
                "group_count": 0, "status": "running", "batch_mode": "STANDALONE",
                "submitted_at": "2026-05-12T00:00:00Z", "run_mode": "CHECK_RUN",
                "operator_id": None, "deployment_profile": None,
            },
        )

    orig_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    client = OrcaClient(_local_daemon_url())
    record = client.submit_workflow("wf", run_mode="CHECK_RUN")
    assert isinstance(record, ExecutionRecordModel)
    assert record.id == "exec-1"
    assert record.status == "running"  # D7: str on the Protocol DTO
