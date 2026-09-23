"""CLI tests for `orca manual-step list` + `confirm`.

list is global by default and scopes with --execution; confirm is SAFE
(no prompt) and routes to client.manual_step_confirm. Both verbs are
backend-aware: local vs cloud is chosen via active_backend(), so the
same fake client stands in for either path.
"""

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.control_plane import ExecutionRecordDTO
from orca.operations.manual_step_models import PendingManualStepDTO


runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.list_called_with: object = "unset"
        self.confirm_called_with: tuple[str, str] | None = None
        self.executions = [
            ExecutionRecordDTO(
                id="exec-1234abcd", workflow_name="smc", status="running",
            ),
        ]
        self.pending = [
            PendingManualStepDTO(
                execution_id="exec-1234abcd",
                step_id="manual_step-aaaa",
                instruction="Centrifuge plate at 300g",
                emitted_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
            ),
        ]

    def list_executions(self) -> list[ExecutionRecordDTO]:
        return self.executions

    def manual_steps_list(
        self, execution_id: str | None,
    ) -> list[PendingManualStepDTO]:
        self.list_called_with = execution_id
        return self.pending

    def manual_step_confirm(self, execution_id: str, step_id: str) -> None:
        self.confirm_called_with = (execution_id, step_id)


def _patch(monkeypatch: pytest.MonkeyPatch, backend: str, stub: _FakeClient) -> None:
    monkeypatch.setattr("orca.cli.manual_step.active_backend", lambda: backend)
    monkeypatch.setattr("orca.cli.manual_step.cloud_client", lambda: stub)
    monkeypatch.setattr("orca.cli.manual_step.local_client", lambda: stub)


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    _patch(monkeypatch, "local", stub)
    yield stub


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    _patch(monkeypatch, "cloud", stub)
    yield stub


def test_list_global_passes_none(local: _FakeClient) -> None:
    result = runner.invoke(app, ["manual-step", "list"])
    assert result.exit_code == 0, result.output
    assert local.list_called_with is None
    assert "manual_step-aaaa" in result.output
    assert "Centrifuge plate" in result.output


def test_list_scoped_resolves_prefix(local: _FakeClient) -> None:
    result = runner.invoke(app, ["manual-step", "list", "--execution", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert local.list_called_with == "exec-1234abcd"


def test_list_json_mode(local: _FakeClient) -> None:
    result = runner.invoke(app, ["--json", "manual-step", "list"])
    assert result.exit_code == 0, result.output
    assert "manual_step-aaaa" in result.output
    assert "exec-1234abcd" in result.output


def test_list_routes_to_cloud(cloud: _FakeClient) -> None:
    result = runner.invoke(app, ["manual-step", "list"])
    assert result.exit_code == 0, result.output
    assert cloud.list_called_with is None
    assert "manual_step-aaaa" in result.output


def test_confirm_routes_to_client_no_prompt(local: _FakeClient) -> None:
    # No -y flag and no stdin: a prompt would hang / fail. SAFE means no prompt.
    result = runner.invoke(app, ["manual-step", "confirm", "manual_step-aaaa",
                                 "--execution", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert local.confirm_called_with == ("exec-1234abcd", "manual_step-aaaa")
    assert "confirmed" in result.output


def test_confirm_routes_to_cloud(cloud: _FakeClient) -> None:
    result = runner.invoke(app, ["manual-step", "confirm", "manual_step-aaaa",
                                 "--execution", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert cloud.confirm_called_with == ("exec-1234abcd", "manual_step-aaaa")


def test_list_all_and_execution_mutually_exclusive(local: _FakeClient) -> None:
    result = runner.invoke(app, ["manual-step", "list", "--all",
                                 "--execution", "exec-1234"])
    assert result.exit_code != 0
