"""CLI cloud-backend parity for verbs that used to be hardcoded local.

`execution close`, `execution threads`, `method list`, `workflow list`,
`incident list/get/ack/ack-all`, `execution pause/resume` and `ops-history
get` prefix resolution all route to the cloud control-plane client when
`active_backend()` is "cloud". A later wave added
`audit list` and the seven `execution thread` mutation verbs (skip/abort/
insert method+action, replace method+action) to that set: all are on
`IControlPlaneClient`, so they must honor the active backend instead of
fail-cleaning on cloud. These tests patch the verb-module dispatch helpers
and assert the cloud path is taken.
"""

from collections.abc import Iterator, Sequence

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.control_plane import (
    AuditEntryDTO,
    ExecutionCloseResponseDTO,
    ExecutionDetailDTO,
    ExecutionRecordDTO,
    MethodSummaryDTO,
    OpsHistoryGetResponseDTO,
    ThreadSnapshotDTO,
    WorkflowSummaryDTO,
)
from orca.daemon.schemas import (
    IncidentAckResponse,
    IncidentDTO,
    LocationDTO,
    ResumeAllResultDTO,
    ThreadTemplateDTO,
)
from orca.operations.thread_models import ReplaceResult


runner = CliRunner()


class _FakeCloud:
    """Records calls + returns canned cloud DTOs."""

    def __init__(self) -> None:
        self.close_called_with: str | None = None
        self.pause_called_with: tuple[str, str | None] | None = None
        self.resume_called_with: tuple[str, str | None] | None = None
        self.ack_called_with: str | None = None
        self.ack_all_called: bool = False
        self.ops_history_called_with: str | None = None
        self.mutation_calls: list[tuple[str, tuple[str, str]]] = []
        self.audit_called: bool = False
        self.executions = [
            ExecutionRecordDTO(id="exec-1234abcd", workflow_name="smc", status="running"),
        ]
        self.incidents = [
            IncidentDTO(
                id="inc-5678ef00",
                timestamp=0.0,
                category="CO_LABWARE_TIMEOUT",
                severity="ERROR",
                message="plate_1 never arrived",
                detail={},
                acknowledged=False,
                execution_id="exec-1234abcd",
                thread_id="thr-aaaa1111",
                recovery_action="",
            ),
        ]

    def list_executions(self) -> list[ExecutionRecordDTO]:
        return self.executions

    def list_workflows(self) -> list[WorkflowSummaryDTO]:
        return [
            WorkflowSummaryDTO(name="smc_assay"),
            WorkflowSummaryDTO(name="calibration"),
        ]

    def incidents_list(
        self,
        unacknowledged_only: bool = False,
        category: str | None = None,
        execution_id: str | None = None,
    ) -> list[IncidentDTO]:
        return self.incidents

    def incidents_get(self, incident_id: str) -> IncidentDTO:
        return self.incidents[0]

    def incidents_ack(self, incident_id: str) -> IncidentAckResponse:
        self.ack_called_with = incident_id
        return IncidentAckResponse(acknowledged_count=1)

    def incidents_ack_all(self, category: str | None = None) -> IncidentAckResponse:
        self.ack_all_called = True
        return IncidentAckResponse(acknowledged_count=3)

    def pause_all_threads(self, execution_id: str, reason: str | None = None) -> None:
        self.pause_called_with = (execution_id, reason)

    def resume_all_threads(
        self, execution_id: str, reason: str | None = None,
    ) -> ResumeAllResultDTO:
        self.resume_called_with = (execution_id, reason)
        return ResumeAllResultDTO(
            resumed=2, pause_cancelled=0, error_skipped=0, completed_skipped=0,
        )

    def ops_history_get(self, execution_id: str) -> OpsHistoryGetResponseDTO:
        self.ops_history_called_with = execution_id
        return OpsHistoryGetResponseDTO(execution_id=execution_id, records=[])

    def execution_close(self, execution_id: str) -> ExecutionCloseResponseDTO:
        self.close_called_with = execution_id
        return ExecutionCloseResponseDTO(execution_id=execution_id, phase="draining")

    def get_execution(self, execution_id: str) -> ExecutionDetailDTO:
        return ExecutionDetailDTO(
            id=execution_id,
            workflow_name="smc",
            status="running",
            threads=[
                ThreadSnapshotDTO(
                    id="thr-aaaa1111",
                    name="plate_1_journey",
                    status="running",
                    completed_method_count=2,
                    current_method={"name": "incubate"},
                ),
            ],
        )

    def methods_list(self) -> list[MethodSummaryDTO]:
        return [
            MethodSummaryDTO(name="incubate", failure_policy="PAUSE", workflow_name="smc"),
            MethodSummaryDTO(name="transfer", failure_policy="ABORT_THREAD", workflow_name="smc"),
        ]

    def list_thread_templates(self) -> list[ThreadTemplateDTO]:
        return [
            ThreadTemplateDTO(
                workflow_name="smc", name="plate_1_journey",
                labware_template_name="plate_1", start_position_id="start",
                end_position_ids=("waste",),
            ),
        ]

    def list_locations(self) -> list[LocationDTO]:
        return [
            LocationDTO(name="incubator", resource_name="inc_1", loaded_labware_ids=()),
        ]

    def thread_skip_method(
        self, execution_id: str, thread_id: str, *, method_name: str, reason: str,
    ) -> None:
        self.mutation_calls.append(("skip_method", (execution_id, thread_id)))

    def thread_abort_method(
        self, execution_id: str, thread_id: str, *, method_name: str, reason: str,
    ) -> None:
        self.mutation_calls.append(("abort_method", (execution_id, thread_id)))

    def thread_insert_method(
        self, execution_id: str, thread_id: str, *, template_name: str | None = None,
        method_code: str | None = None, where: str, anchor: str | None = None,
        reason: str,
    ) -> None:
        self.mutation_calls.append(("insert_method", (execution_id, thread_id)))

    def thread_skip_action(
        self, execution_id: str, thread_id: str, *, action_id: str | None = None,
        action_command: str | None = None, reason: str,
    ) -> None:
        self.mutation_calls.append(("skip_action", (execution_id, thread_id)))

    def thread_insert_action(
        self, execution_id: str, thread_id: str, *, action_code: str, where: str,
        anchor: str | None = None, reason: str,
    ) -> None:
        self.mutation_calls.append(("insert_action", (execution_id, thread_id)))

    def thread_replace_method(
        self, execution_id: str, thread_id: str, *, target_name: str,
        template_name: str | None = None, method_code: str | None = None,
        reason: str,
    ) -> ReplaceResult:
        self.mutation_calls.append(("replace_method", (execution_id, thread_id)))
        return ReplaceResult(execution_id=execution_id, thread_id=thread_id)

    def thread_replace_action(
        self, execution_id: str, thread_id: str, *, target_command: str,
        action_code: str, reason: str,
    ) -> ReplaceResult:
        self.mutation_calls.append(("replace_action", (execution_id, thread_id)))
        return ReplaceResult(execution_id=execution_id, thread_id=thread_id)

    def audit_list(
        self, *, action_name: str | None = None, limit: int = 200,
    ) -> Sequence[AuditEntryDTO]:
        self.audit_called = True
        return [
            AuditEntryDTO(
                timestamp=1_700_000_000.0,
                action_name="thread.skip_method",
                danger_level="critical",
                reason="cloud audit entry",
            ),
        ]


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeCloud]:
    stub = _FakeCloud()
    monkeypatch.setattr("orca.cli.execution.active_backend", lambda: "cloud")
    monkeypatch.setattr("orca.cli.execution.cloud_client", lambda: stub)
    monkeypatch.setattr("orca.cli.execution.get_client", lambda: stub)
    monkeypatch.setattr("orca.cli.registry.active_backend", lambda: "cloud")
    monkeypatch.setattr("orca.cli.registry.cloud_client", lambda: stub)
    monkeypatch.setattr("orca.cli.registry.get_client", lambda: stub)
    monkeypatch.setattr("orca.cli.incident.active_backend", lambda: "cloud")
    monkeypatch.setattr("orca.cli.incident.cloud_client", lambda: stub)
    monkeypatch.setattr("orca.cli.ops_history.get_client", lambda: stub)
    monkeypatch.setattr("orca.cli.audit.get_client", lambda: stub)
    monkeypatch.setattr("orca.cli.describe.get_client", lambda: stub)
    yield stub


def test_execution_close_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", "execution", "close", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert cloud.close_called_with == "exec-1234abcd"
    assert "draining" in result.output


def test_execution_threads_routes_to_cloud_detail(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["execution", "threads", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert "plate_1_journey" in result.output
    assert "incubate" in result.output


def test_method_list_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["method", "list"])
    assert result.exit_code == 0, result.output
    assert "incubate" in result.output
    assert "transfer" in result.output
    assert "PAUSE" in result.output


def test_workflow_list_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["workflow", "list"])
    assert result.exit_code == 0, result.output
    assert "smc_assay" in result.output
    assert "calibration" in result.output


def test_describe_method_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["describe", "method", "incubate"])
    assert result.exit_code == 0, result.output
    assert "incubate" in result.output
    assert "PAUSE" in result.output


def test_describe_thread_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["describe", "thread", "plate_1_journey"])
    assert result.exit_code == 0, result.output
    assert "plate_1_journey" in result.output
    assert "plate_1" in result.output


def test_describe_location_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["describe", "location", "incubator"])
    assert result.exit_code == 0, result.output
    assert "incubator" in result.output
    assert "inc_1" in result.output


def test_incident_list_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["incident", "list"])
    assert result.exit_code == 0, result.output
    assert "CO_LABWARE_TIMEOUT" in result.output


def test_incident_get_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["incident", "get", "inc-5678"])
    assert result.exit_code == 0, result.output
    assert "plate_1 never arrived" in result.output


def test_incident_ack_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", "incident", "ack", "inc-5678"])
    assert result.exit_code == 0, result.output
    assert cloud.ack_called_with == "inc-5678ef00"


def test_incident_ack_all_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", "incident", "ack-all"])
    assert result.exit_code == 0, result.output
    assert cloud.ack_all_called is True
    assert "3 incidents" in result.output


def test_execution_pause_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", "execution", "pause", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert cloud.pause_called_with == ("exec-1234abcd", None)


def test_execution_resume_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["execution", "resume", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert cloud.resume_called_with == ("exec-1234abcd", None)
    assert "resumed" in result.output


def test_ops_history_get_resolves_prefix_on_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["ops-history", "get", "exec-1234"])
    assert result.exit_code == 0, result.output
    assert cloud.ops_history_called_with == "exec-1234abcd"


# -- audit + thread-mutation parity (this wave) -------------------------------


_THREAD = ["execution", "thread"]


def test_audit_list_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["audit", "list"])
    assert result.exit_code == 0, result.output
    assert cloud.audit_called is True
    assert "thread.skip_method" in result.output


def test_skip_method_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", *_THREAD, "skip-method", "exec-1234",
                                 "thr-aaaa", "--method-name", "incubate",
                                 "--reason", "test"])
    assert result.exit_code == 0, result.output
    assert cloud.mutation_calls == [("skip_method", ("exec-1234abcd", "thr-aaaa1111"))]


def test_abort_method_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", *_THREAD, "abort-method", "exec-1234",
                                 "thr-aaaa", "--method-name", "incubate",
                                 "--reason", "test"])
    assert result.exit_code == 0, result.output
    assert cloud.mutation_calls == [("abort_method", ("exec-1234abcd", "thr-aaaa1111"))]


def test_insert_method_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", *_THREAD, "insert-method", "exec-1234",
                                 "thr-aaaa", "--template", "incubate",
                                 "--where", "tail", "--reason", "test"])
    assert result.exit_code == 0, result.output
    assert cloud.mutation_calls == [("insert_method", ("exec-1234abcd", "thr-aaaa1111"))]


def test_skip_action_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", *_THREAD, "skip-action", "exec-1234",
                                 "thr-aaaa", "--action-command", "seal",
                                 "--reason", "test"])
    assert result.exit_code == 0, result.output
    assert cloud.mutation_calls == [("skip_action", ("exec-1234abcd", "thr-aaaa1111"))]


def test_insert_action_routes_to_cloud(cloud: _FakeCloud, tmp_path) -> None:
    src = tmp_path / "act.py"
    src.write_text("@orca.action\ndef a():\n    pass\n", encoding="utf-8")
    result = runner.invoke(app, ["-y", *_THREAD, "insert-action", "exec-1234",
                                 "thr-aaaa", str(src), "--where", "tail",
                                 "--reason", "test"])
    assert result.exit_code == 0, result.output
    assert cloud.mutation_calls == [("insert_action", ("exec-1234abcd", "thr-aaaa1111"))]


def test_replace_method_routes_to_cloud(cloud: _FakeCloud) -> None:
    result = runner.invoke(app, ["-y", *_THREAD, "replace-method", "exec-1234",
                                 "thr-aaaa", "seal", "--template", "incubate",
                                 "--reason", "test"])
    assert result.exit_code == 0, result.output
    assert cloud.mutation_calls == [("replace_method", ("exec-1234abcd", "thr-aaaa1111"))]


def test_replace_action_routes_to_cloud(cloud: _FakeCloud, tmp_path) -> None:
    src = tmp_path / "act.py"
    src.write_text("@orca.action\ndef a():\n    pass\n", encoding="utf-8")
    result = runner.invoke(app, ["-y", *_THREAD, "replace-action", "exec-1234",
                                 "thr-aaaa", "seal", str(src), "--reason", "test"])
    assert result.exit_code == 0, result.output
    assert cloud.mutation_calls == [("replace_action", ("exec-1234abcd", "thr-aaaa1111"))]
