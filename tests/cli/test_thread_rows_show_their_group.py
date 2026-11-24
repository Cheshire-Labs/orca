"""`orca execution threads` and `orca execution thread detail` show the labware group each thread serves."""

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.operations.execution_models import ExecutionRecordModel, GetExecutionDetailResponse
from orca.operations.wire_models import ThreadSnapshotResponse

runner = CliRunner()

_THREAD = ThreadSnapshotResponse(
    id="thr-aaaa1111", name="sample_1", status="RUNNING", current_location="pad_1", current_method=None,
    completed_method_count=0, last_error=None, pause_reason=None, completed_methods=(), group_id="group-seven",
)


class _Client:
    def list_executions(self) -> list[ExecutionRecordModel]:
        return [ExecutionRecordModel(id="exec-1234abcd", workflow_name="wf", status="running")]

    def list_threads(self, execution_id: str) -> list[ThreadSnapshotResponse]:
        return [_THREAD]

    def get_execution(self, execution_id: str) -> GetExecutionDetailResponse:
        return GetExecutionDetailResponse(id=execution_id, workflow_name="wf", status="running", threads=[_THREAD])

    def get_thread_detail(self, execution_id: str, thread_id: str) -> ThreadSnapshotResponse:
        return _THREAD


@pytest.fixture(autouse=True)
def _client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("orca.cli.execution.get_client", _Client)
    yield


@pytest.mark.parametrize("verb", [["execution", "threads", "exec-1234"], ["execution", "thread", "detail", "exec-1234", "thr-aaaa"]])
def test_the_threads_group_is_shown(verb: list[str]) -> None:
    result = runner.invoke(app, verb, terminal_width=200)

    assert result.exit_code == 0, result.output
    assert "group-seven" in result.output
