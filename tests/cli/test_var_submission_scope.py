"""`orca var` reaches the submission layer and says when it is shadowed.

A value supplied on a submit outranks the per-execution partition, so a set
that reports success can leave the run resolving something else. The verbs
below give the operator both halves: the warning that it happened, and the
`--submission` switch that reaches the layer that wins.
"""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from orca.cli.app import app
from orca.daemon.schemas import (
    ExecutionRecordDTO,
    SubmissionOverrideDTO,
    VariableResolutionResponse,
    VariableSetResponse,
    VariableValue,
)
from orca.variables.resolution import VariableSource


runner = CliRunner()

EXECUTION_ID = "11111111-2222-3333-4444-555555555555"
SUBMISSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _client(mock_get_client: MagicMock) -> MagicMock:
    client = mock_get_client.return_value
    client.list_executions.return_value = [
        ExecutionRecordDTO(
            id=EXECUTION_ID, workflow_name="smc_assay", status="running", error=None,
        ),
    ]
    return client


class TestSetWarnsWhenASubmissionStillWins:

    def test_it_names_the_submission_that_outranks_the_write(self) -> None:
        with patch("orca.cli.var.get_client") as mock_get_client:
            client = _client(mock_get_client)
            client.variables_set.return_value = VariableSetResponse(
                name="inject_fault", value=False, shadowed_by=[SUBMISSION_ID],
            )

            result = runner.invoke(
                app,
                ["--force", "var", "set", "inject_fault", "false",
                 "--execution", EXECUTION_ID],
            )

        assert result.exit_code == 0, result.output
        assert SUBMISSION_ID[:8] in result.output
        # The hint has to be a command that runs: both verbs need --execution.
        hint = " ".join(result.output.split())
        assert f"orca var explain inject_fault --execution {EXECUTION_ID}" in hint
        assert (
            f"orca var unset inject_fault --execution {EXECUTION_ID} "
            f"--submission <id>"
        ) in hint

    def test_it_stays_quiet_when_nothing_shadows_the_write(self) -> None:
        with patch("orca.cli.var.get_client") as mock_get_client:
            client = _client(mock_get_client)
            client.variables_set.return_value = VariableSetResponse(
                name="inject_fault", value=False,
            )

            result = runner.invoke(
                app,
                ["--force", "var", "set", "inject_fault", "false",
                 "--execution", EXECUTION_ID],
            )

        assert result.exit_code == 0, result.output
        assert "overridden by submission" not in result.output

    def test_the_submission_switch_writes_the_layer_that_wins(self) -> None:
        with patch("orca.cli.var.get_client") as mock_get_client:
            client = _client(mock_get_client)

            result = runner.invoke(
                app,
                ["--force", "var", "set", "inject_fault", "false",
                 "--execution", EXECUTION_ID, "--submission", SUBMISSION_ID],
            )

        assert result.exit_code == 0, result.output
        client.variables_set_submission.assert_called_once_with(
            EXECUTION_ID, SUBMISSION_ID, "inject_fault", False,
        )
        client.variables_set.assert_not_called()


class TestUnsetReachesTheSubmissionLayer:

    def test_the_submission_switch_clears_that_submissions_override(self) -> None:
        with patch("orca.cli.var.get_client") as mock_get_client:
            client = _client(mock_get_client)

            result = runner.invoke(
                app,
                ["--force", "var", "unset", "inject_fault",
                 "--execution", EXECUTION_ID, "--submission", SUBMISSION_ID],
            )

        assert result.exit_code == 0, result.output
        client.variables_unset_submission.assert_called_once_with(
            EXECUTION_ID, SUBMISSION_ID, "inject_fault",
        )
        client.variables_unset.assert_not_called()


class TestGetSaysWhereTheValueCameFrom:

    def test_it_reports_the_layer_and_warns_about_submission_overrides(self) -> None:
        with patch("orca.cli.var.get_client") as mock_get_client:
            client = _client(mock_get_client)
            client.variables_get.return_value = VariableValue(
                name="inject_fault", execution_id=EXECUTION_ID, value=False,
                source=VariableSource.EXECUTION, shadowed_by=[SUBMISSION_ID],
            )

            result = runner.invoke(
                app, ["var", "get", "inject_fault", "--execution", EXECUTION_ID],
            )

        assert result.exit_code == 0, result.output
        assert "execution" in result.output
        assert SUBMISSION_ID[:8] in result.output

    def test_the_submission_switch_reads_that_submissions_value(self) -> None:
        with patch("orca.cli.var.get_client") as mock_get_client:
            client = _client(mock_get_client)
            client.variables_get.return_value = VariableValue(
                name="inject_fault", execution_id=EXECUTION_ID, value=True,
                source=VariableSource.SUBMISSION,
            )

            result = runner.invoke(
                app,
                ["var", "get", "inject_fault", "--execution", EXECUTION_ID,
                 "--submission", SUBMISSION_ID],
            )

        assert result.exit_code == 0, result.output
        client.variables_get.assert_called_once_with(
            EXECUTION_ID, "inject_fault", SUBMISSION_ID,
        )


class TestExplainListsEverySubmission:

    def test_it_prints_each_submissions_own_value(self) -> None:
        with patch("orca.cli.var.get_client") as mock_get_client:
            client = _client(mock_get_client)
            client.variables_resolution.return_value = VariableResolutionResponse(
                name="shake_time", execution_id=EXECUTION_ID, value=60,
                source=VariableSource.WORKFLOW_DEFAULT,
                overrides=[
                    SubmissionOverrideDTO(submission_id=SUBMISSION_ID, value=120),
                ],
            )

            result = runner.invoke(
                app, ["var", "explain", "shake_time", "--execution", EXECUTION_ID],
            )

        assert result.exit_code == 0, result.output
        assert "workflow-default" in result.output
        assert SUBMISSION_ID[:8] in result.output
        assert "120" in result.output
