"""LocalDaemonClient round-trip tests against a fake daemon.

Uses `httpx.MockTransport` (sync) to serve canned responses for the daemon
routes it exercises. Covers the Protocol-shape methods plus the
`device_introspection` route and the `health` endpoint.
"""

import json
from contextlib import contextmanager
from typing import Iterator

import httpx
import pytest
import typer

from orca.cli import output

from orca.cli.client import LocalDaemonClient
from orca.cli.control_plane import (
    DeviceDTO,
    DeviceIntrospectionDTO,
    ExecutionDetailDTO,
    ExecutionRecordDTO,
)
from orca.runtime.run_modes import WorkflowRunMode


def _fake_daemon_handler(request: httpx.Request) -> httpx.Response:
    method = request.method
    path = request.url.path
    if method == "GET" and path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if method == "POST" and path == "/executions":
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "id": "abc",
            "workflow_name": body.get("workflow_name"),
            "status": "submitted",
            "error": None,
        })
    if method == "GET" and path == "/operations/list-executions":
        return httpx.Response(200, json={"executions": [
            {"id": "abc", "workflow_name": "smc", "status": "running", "error": None},
        ]})
    if method == "POST" and path == "/operations/get-execution-detail":
        body = json.loads(request.content.decode("utf-8"))
        eid = body.get("execution_id")
        return httpx.Response(200, json={
            "id": eid,
            "workflow_name": "smc",
            "status": "running",
            "error": None,
            "threads": [],
            "total_thread_count": 3,
            "completed_thread_count": 1,
            "active_thread_count": 2,
        })
    if method == "POST" and path == "/operations/stop-execution":
        body = json.loads(request.content.decode("utf-8"))
        eid = body.get("execution_id")
        confirmed = bool(body.get("confirm"))
        if confirmed:
            return httpx.Response(200, json={
                "status": "aborted", "execution_id": eid, "phase": "aborted",
                "message": f"execution {eid} aborted",
            })
        return httpx.Response(200, json={
            "status": "armed", "execution_id": eid, "phase": "accepting",
            "message": "execution paused and abort armed",
        })
    if method == "POST" and path == "/operations/remove-execution":
        body = json.loads(request.content.decode("utf-8"))
        eid = body.get("execution_id")
        return httpx.Response(200, json={"status": "removed", "execution_id": eid})
    if method == "GET" and path == "/catalog/devices":
        return httpx.Response(200, json=[
            {
                "name": "shaker1",
                "type_name": "Shaker",
                "is_initialized": True,
                "is_busy": False,
                "effective_mode": "PURE_SIM",
                "position_ids": ["loc1"],
                "loaded_labware_ids": [],
            },
        ])
    if method == "GET" and path.startswith("/devices/") and path.endswith("/introspection"):
        name = path.split("/")[2]
        return httpx.Response(200, json={
            "type": "SimShaker",
            "name": name,
            "interfaces": ["IShaker"],
            "capabilities": ["connect"],
            "provides_state": False,
            "methods": {
                "shake": {"kind": "method", "params": {}, "returns": "None"},
            },
        })
    return httpx.Response(404, json={"detail": f"unmocked: {method} {path}"})


class _FakeDaemonClient(LocalDaemonClient):
    """Test subclass that swaps the underlying httpx.Client construction.

    Stays on the public class so `isinstance(daemon, LocalDaemonClient)` keeps
    working; only the override-point `_make_http_client` flips the transport.
    """

    def __init__(self, transport: httpx.BaseTransport) -> None:
        from orca.daemon.lifecycle import DaemonInfo
        self._info = DaemonInfo(pid=0, port=0, started_at=0.0)
        self._base_url = "http://test-daemon"
        self._timeout = 30.0
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            base_url=self._base_url,
            timeout=self._timeout,
        )


@pytest.fixture
def daemon_client() -> LocalDaemonClient:
    """LocalDaemonClient wired to an `httpx.MockTransport`."""
    return _FakeDaemonClient(httpx.MockTransport(_fake_daemon_handler))


def test_health_returns_true_on_2xx(daemon_client: LocalDaemonClient) -> None:
    assert daemon_client.health() is True


def test_health_returns_false_on_transport_error() -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    inst = _FakeDaemonClient(httpx.MockTransport(fail))
    assert inst.health() is False


def test_submit_workflow_returns_protocol_dto_with_str_status(
    daemon_client: LocalDaemonClient,
) -> None:
    """D7: status is `str` on the Protocol DTO returned to CLI verbs."""
    record = daemon_client.submit_workflow("smc", {"plate_count": 4}, run_mode="PURE_SIM")
    assert isinstance(record, ExecutionRecordDTO)
    assert record.id == "abc"
    assert isinstance(record.status, str)
    assert record.status == "submitted"


def test_submit_workflow_accepts_optional_profile_path(
    daemon_client: LocalDaemonClient,
) -> None:
    """`profile_path` is daemon-only but accepted (for `orca run --profile`)."""
    record = daemon_client.submit_workflow(
        "smc", {"x": 1}, profile_path="/tmp/profile.json",
        run_mode="PURE_SIM",
    )
    assert isinstance(record, ExecutionRecordDTO)


def test_list_executions_returns_protocol_dtos(
    daemon_client: LocalDaemonClient,
) -> None:
    records = list(daemon_client.list_executions())
    assert len(records) == 1
    assert isinstance(records[0], ExecutionRecordDTO)
    assert records[0].status == "running"


def test_get_execution_returns_rich_detail_per_d2(
    daemon_client: LocalDaemonClient,
) -> None:
    detail = daemon_client.get_execution("abc")
    assert isinstance(detail, ExecutionDetailDTO)
    assert detail.total_thread_count == 3
    assert detail.completed_thread_count == 1
    assert detail.active_thread_count == 2
    assert isinstance(detail.status, str)


def test_list_devices_returns_minimal_protocol_shape(
    daemon_client: LocalDaemonClient,
) -> None:
    """The Protocol-shape `list_devices` is name + type only."""
    devices = list(daemon_client.list_devices())
    assert len(devices) == 1
    assert isinstance(devices[0], DeviceDTO)
    assert devices[0].name == "shaker1"
    assert devices[0].type_name == "Shaker"
    assert not hasattr(devices[0], "is_busy")


def test_list_device_snapshots_returns_rich_shape(
    daemon_client: LocalDaemonClient,
) -> None:
    snaps = daemon_client.list_device_snapshots()
    assert len(snaps) == 1
    assert snaps[0].name == "shaker1"
    assert snaps[0].is_busy is False
    assert snaps[0].is_initialized is True


def test_device_introspection_round_trip_per_d1(
    daemon_client: LocalDaemonClient,
) -> None:
    """D1: daemon's NEW /devices/{name}/introspection returns the introspection shape."""
    info = daemon_client.device_introspection("shaker1")
    assert isinstance(info, DeviceIntrospectionDTO)
    assert info.name == "shaker1"
    assert info.type == "SimShaker"
    assert info.interfaces == ["IShaker"]
    assert info.capabilities == ["connect"]
    assert info.provides_state is False
    assert "shake" in info.methods


def _fail_with(error: httpx.HTTPError) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error
    return httpx.MockTransport(handler)


def _exit_and_message(
    inst: LocalDaemonClient, capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    with pytest.raises(typer.Exit) as exit_info:
        inst.mount_topology("some.module:build_topology")
    printed = capsys.readouterr()
    return exit_info.value.exit_code, printed.out + printed.err


def test_a_daemon_that_refuses_the_connection_is_reported_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed connect is the one transport error that means no daemon."""
    inst = _FakeDaemonClient(_fail_with(httpx.ConnectError("refused")))

    code, message = _exit_and_message(inst, capsys)

    assert code == output.EXIT_NOT_CONNECTED
    assert "cannot reach the daemon" in message


def test_a_daemon_that_does_not_answer_in_time_is_not_reported_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A timeout says the work may still be running, not that the daemon is gone.

    Calling it unreachable sends the operator to restart a daemon that is
    still building, or to retry into a 409 once the build lands.
    """
    inst = _FakeDaemonClient(_fail_with(httpx.ReadTimeout("took too long")))

    code, message = _exit_and_message(inst, capsys)

    assert code == output.EXIT_TIMEOUT
    assert "did not answer within" in message
    assert "may still complete" in message
    assert "cannot reach" not in message


def test_a_dropped_connection_says_the_request_may_have_completed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    inst = _FakeDaemonClient(_fail_with(httpx.RemoteProtocolError("dropped")))

    code, message = _exit_and_message(inst, capsys)

    assert code == output.EXIT_NOT_CONNECTED
    assert "lost the connection to the daemon" in message
    assert "may or may not have completed" in message


def _recording_read_limits() -> tuple[LocalDaemonClient, dict[str, float | None]]:
    """A client whose daemon refuses every request, after recording its read limit by path."""
    read_timeouts: dict[str, float | None] = {}

    def record(request: httpx.Request) -> httpx.Response:
        read_timeouts[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(500, json={"detail": "recorded"})

    return _FakeDaemonClient(httpx.MockTransport(record)), read_timeouts


def test_mount_and_workflow_load_wait_longer_than_an_ordinary_verb() -> None:
    """Both import user code, and the first import on a fresh install also compiles it."""
    inst, read_timeouts = _recording_read_limits()

    with pytest.raises(typer.Exit):
        inst.mount_topology("some.module:build_topology")
    with pytest.raises(typer.Exit):
        inst.load_workflow("some.module:build_workflow")
    with pytest.raises(typer.Exit):
        inst.list_executions()

    assert read_timeouts["/operations/list-executions"] == 30.0
    for path in ("/mount-topology", "/workflows"):
        limit = read_timeouts[path]
        assert limit is not None and limit >= 600.0, path


def test_a_device_command_waits_as_long_as_the_daemon_lets_it_run() -> None:
    """The daemon times each command from the driver's duration; a shake can run for hours."""
    inst, read_timeouts = _recording_read_limits()

    with pytest.raises(typer.Exit):
        inst.device_execute("shaker_1", "shake")
    with pytest.raises(typer.Exit):
        inst.device_invoke("shaker_1", "shake")
    with pytest.raises(typer.Exit):
        inst.device_initialize("shaker_1")
    with pytest.raises(typer.Exit):
        inst.device_connect("shaker_1")
    with pytest.raises(typer.Exit):
        inst.device_disconnect("shaker_1")
    with pytest.raises(typer.Exit):
        inst.device_reconcile_deck("ml_star")
    with pytest.raises(typer.Exit):
        inst.device_compare_deck("ml_star")

    assert len(read_timeouts) == 7
    assert set(read_timeouts.values()) == {None}
