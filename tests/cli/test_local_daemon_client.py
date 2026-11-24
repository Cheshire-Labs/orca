"""OrcaClient round-trip tests against a fake daemon.

Uses `httpx.MockTransport` (sync) to serve canned responses for the daemon
routes it exercises. Covers the Protocol-shape methods plus the
`device_introspection` route and the `health` endpoint.
"""

import json

import httpx
import pytest

from orca.cli import output

from orca.cli.control_plane import ControlPlaneError
from orca.cli.daemon_client import DaemonLoadingClient
from orca.operations.operator_device_models import BATCH_EXECUTE, BatchExecuteRequest
from orca.operations.wire_models import (
    DeviceIntrospectionDTO,
)
from orca.operations.execution_models import (
    GetExecutionDetailResponse,
    ExecutionRecordModel,
)


def _fake_daemon_handler(request: httpx.Request) -> httpx.Response:
    method = request.method
    path = request.url.path
    if method == "GET" and path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if method == "POST" and path == "/api/executions":
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "id": "sub-abc",
            "execution_id": "abc",
            "workflow_name": body.get("workflow_name"),
            "group_count": 0,
            "status": "submitted",
            "batch_mode": "STANDALONE",
            "submitted_at": "2026-05-12T00:00:00Z",
            "run_mode": body.get("run_mode"),
            "operator_id": None,
            "deployment_profile": None,
        })
    if method == "GET" and path == "/api/executions":
        return httpx.Response(200, json={"executions": [
            {"id": "abc", "workflow_name": "smc", "status": "running", "error": None},
        ]})
    if method == "GET" and path.startswith("/api/executions/"):
        eid = path.split("/")[3]
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
    if method == "POST" and path.startswith("/api/executions/") and path.endswith("/stop"):
        body = json.loads(request.content.decode("utf-8"))
        eid = path.split("/")[3]
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
    if method == "DELETE" and path.startswith("/api/executions/"):
        eid = path.split("/")[3]
        return httpx.Response(200, json={"status": "removed", "execution_id": eid})
    if method == "GET" and path.startswith("/api/devices/") and path.endswith("/introspection"):
        name = path.split("/")[3]
        return httpx.Response(200, json={
            "type": "SimShaker",
            "name": name,
            "interfaces": ["IShaker"],
            "capabilities": ["connect"],
            "provides_state": False,
            "methods": {
                "shake": {"kind": "method", "params": {}, "returns": "None"},
            },
            "danger_levels": {"shaker.shake": "PHYSICAL"},
        })
    return httpx.Response(404, json={"detail": f"unmocked: {method} {path}"})


class _FakeDaemonClient(DaemonLoadingClient):
    """Test subclass that swaps the underlying httpx.Client construction.

    Stays on the public class so `isinstance(daemon, OrcaClient)` keeps
    working; only the override-point `_make_http_client` flips the transport.
    """

    def __init__(self, transport: httpx.BaseTransport) -> None:
        super().__init__("http://test-daemon", timeout=30.0)
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            base_url=self._base_url,
            timeout=self._timeout,
        )


@pytest.fixture
def daemon_client() -> DaemonLoadingClient:
    """OrcaClient wired to an `httpx.MockTransport`."""
    return _FakeDaemonClient(httpx.MockTransport(_fake_daemon_handler))


def test_health_returns_true_on_2xx(daemon_client: DaemonLoadingClient) -> None:
    assert daemon_client.health() is True


def test_health_returns_false_on_transport_error() -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    inst = _FakeDaemonClient(httpx.MockTransport(fail))
    assert inst.health() is False


def test_submit_workflow_returns_protocol_dto_with_str_status(
    daemon_client: DaemonLoadingClient,
) -> None:
    """D7: status is `str` on the Protocol DTO returned to CLI verbs."""
    record = daemon_client.submit_workflow("smc", {"plate_count": 4}, run_mode="CHECK_RUN")
    assert isinstance(record, ExecutionRecordModel)
    assert record.id == "abc"
    assert isinstance(record.status, str)
    assert record.status == "submitted"


def test_submit_workflow_accepts_an_optional_profile_name(
    daemon_client: DaemonLoadingClient,
) -> None:
    """`orca run --profile <name>` sends the name."""
    record = daemon_client.submit_workflow(
        "smc", {"x": 1}, deployment_profile="qc",
        run_mode="CHECK_RUN",
    )
    assert isinstance(record, ExecutionRecordModel)


def test_list_executions_returns_protocol_dtos(
    daemon_client: DaemonLoadingClient,
) -> None:
    records = list(daemon_client.list_executions())
    assert len(records) == 1
    assert isinstance(records[0], ExecutionRecordModel)
    assert records[0].status == "running"


def test_get_execution_returns_rich_detail_per_d2(
    daemon_client: DaemonLoadingClient,
) -> None:
    detail = daemon_client.get_execution("abc")
    assert isinstance(detail, GetExecutionDetailResponse)
    assert detail.total_thread_count == 3
    assert detail.completed_thread_count == 1
    assert detail.active_thread_count == 2
    assert isinstance(detail.status, str)


def test_device_introspection_round_trip_per_d1(
    daemon_client: DaemonLoadingClient,
) -> None:
    """D1: the daemon's /api/devices/{device_id}/introspection returns the introspection shape."""
    info = daemon_client.device_introspection("shaker1")
    assert isinstance(info, DeviceIntrospectionDTO)
    assert info.name == "shaker1"
    assert info.type == "SimShaker"
    assert info.interfaces == ("IShaker",)
    assert info.capabilities == ("connect",)
    assert info.provides_state is False
    assert "shake" in info.methods
    assert info.danger_levels == {"shaker.shake": "PHYSICAL"}


def _fail_with(error: httpx.HTTPError) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error
    return httpx.MockTransport(handler)


def _exit_and_message(
    inst: DaemonLoadingClient,
) -> tuple[int, str]:
    with pytest.raises(ControlPlaneError) as failure:
        inst.mount_package()
    assert failure.value.exit_code is not None
    return failure.value.exit_code, str(failure.value)


def test_a_daemon_that_refuses_the_connection_is_reported_unreachable() -> None:
    """A failed connect is the one transport error that means no daemon."""
    inst = _FakeDaemonClient(_fail_with(httpx.ConnectError("refused")))

    code, message = _exit_and_message(inst)

    assert code == output.EXIT_NOT_CONNECTED
    assert "cannot reach the server" in message


def test_a_daemon_that_does_not_answer_in_time_is_not_reported_unreachable() -> None:
    """A timeout says the work may still be running, not that the daemon is gone.

    Calling it unreachable sends the operator to restart a daemon that is
    still building, or to retry into a 409 once the build lands.
    """
    inst = _FakeDaemonClient(_fail_with(httpx.ReadTimeout("took too long")))

    code, message = _exit_and_message(inst)

    assert code == output.EXIT_TIMEOUT
    assert "did not answer within" in message
    assert "may still complete" in message
    assert "cannot reach" not in message


def test_a_dropped_connection_says_the_request_may_have_completed() -> None:
    inst = _FakeDaemonClient(_fail_with(httpx.RemoteProtocolError("dropped")))

    code, message = _exit_and_message(inst)

    assert code == output.EXIT_NOT_CONNECTED
    assert "lost the connection to the server" in message
    assert "may or may not have completed" in message


def _recording_read_limits() -> tuple[DaemonLoadingClient, dict[str, float | None]]:
    """A client whose daemon refuses every request, after recording its read limit by path."""
    read_timeouts: dict[str, float | None] = {}

    def record(request: httpx.Request) -> httpx.Response:
        read_timeouts[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(500, json={"detail": "recorded"})

    return _FakeDaemonClient(httpx.MockTransport(record)), read_timeouts


def test_a_mount_waits_longer_than_an_ordinary_verb() -> None:
    """A mount imports user code, and the first import on a fresh install also compiles it."""
    inst, read_timeouts = _recording_read_limits()

    with pytest.raises(ControlPlaneError):
        inst.mount_package()
    with pytest.raises(ControlPlaneError):
        inst.list_executions()

    assert read_timeouts["/api/executions"] == 30.0
    assert read_timeouts["/api/runtime/mount-package"] is None


def test_a_device_command_waits_as_long_as_the_daemon_lets_it_run() -> None:
    """The daemon times each command from the driver's duration; a shake can run for hours."""
    inst, read_timeouts = _recording_read_limits()

    with pytest.raises(ControlPlaneError):
        inst.device_operator_command(
            BATCH_EXECUTE,
            BatchExecuteRequest(device_id="shaker_1", commands=[{"command": "shake"}], stop_on_error=True),
        )
    with pytest.raises(ControlPlaneError):
        inst.device_initialize("shaker_1")
    with pytest.raises(ControlPlaneError):
        inst.device_connect("shaker_1")
    with pytest.raises(ControlPlaneError):
        inst.device_disconnect("shaker_1")
    with pytest.raises(ControlPlaneError):
        inst.device_reconcile_deck("ml_star")
    with pytest.raises(ControlPlaneError):
        inst.device_compare_deck("ml_star")

    assert len(read_timeouts) == 6
    assert set(read_timeouts.values()) == {None}
