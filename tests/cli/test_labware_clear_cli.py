"""CLI tests for ``orca labware clear-submission/discharge/clear-all``.

Review item L4 required a confirmation prompt and a non-TTY refusal on
`clear-all` specifically. The other two are scoped to one submission or
one labware id and are less catastrophic on misfire.
"""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orca.cli.app import app


runner = CliRunner()


class TestClearAllConfirmation:

    def test_non_tty_without_yes_refuses(self) -> None:
        """On non-TTY (CliRunner stdin is not a TTY), missing --yes
        must abort BEFORE any cloud call."""
        with patch("orca.cli.labware.get_client") as mock_cloud:
            result = runner.invoke(app, ["labware", "clear-all"])
        assert result.exit_code == 2
        assert "non-TTY" in result.output
        mock_cloud.assert_not_called()

    def test_non_tty_with_yes_proceeds(self) -> None:
        """`--yes` on a non-TTY skips the prompt and calls the cloud."""
        with patch("orca.cli.labware.get_client") as mock_cloud:
            mock_cloud.return_value.labware_clear_all.return_value = {
                "cleared_labware_ids": ["lw-1", "lw-2"],
            }
            result = runner.invoke(app, ["labware", "clear-all", "--yes"])
        assert result.exit_code == 0
        assert "cleared 2 labware" in result.output
        mock_cloud.return_value.labware_clear_all.assert_called_once_with(force=False)

    def test_yes_combined_with_force(self) -> None:
        """`--yes --force` is the documented panic recipe."""
        with patch("orca.cli.labware.get_client") as mock_cloud:
            mock_cloud.return_value.labware_clear_all.return_value = {
                "cleared_labware_ids": [],
            }
            result = runner.invoke(
                app, ["labware", "clear-all", "--yes", "--force"],
            )
        assert result.exit_code == 0
        mock_cloud.return_value.labware_clear_all.assert_called_once_with(force=True)

    def test_tty_decline_aborts(self) -> None:
        """On a TTY, answering 'n' to the confirm aborts with exit code 1."""
        with patch("orca.cli.labware._stdin_is_tty", return_value=True), \
             patch("orca.cli.labware.typer.confirm", return_value=False) as confirm, \
             patch("orca.cli.labware.get_client") as mock_cloud:
            result = runner.invoke(app, ["labware", "clear-all"])
        assert result.exit_code == 1, result.output
        assert "aborted" in result.output
        confirm.assert_called_once()
        mock_cloud.assert_not_called()

    def test_tty_accept_proceeds(self) -> None:
        """On a TTY, answering 'y' to the confirm calls the cloud."""
        with patch("orca.cli.labware._stdin_is_tty", return_value=True), \
             patch("orca.cli.labware.typer.confirm", return_value=True), \
             patch("orca.cli.labware.get_client") as mock_cloud:
            mock_cloud.return_value.labware_clear_all.return_value = {
                "cleared_labware_ids": ["lw-1"],
            }
            result = runner.invoke(app, ["labware", "clear-all"])
        assert result.exit_code == 0, result.output
        mock_cloud.return_value.labware_clear_all.assert_called_once_with(force=False)


class TestClearSubmissionNoPromptRequired:
    """clear-submission is scoped to one submission and does NOT require
    a confirmation prompt. The other two tools (discharge / clear-submission)
    only become catastrophic on misfire if combined with force=True on a
    long-running execution -- the active-execution refusal already gates
    that path."""

    def test_no_prompt_on_non_tty(self) -> None:
        with patch("orca.cli.labware.get_client") as mock_cloud:
            mock_cloud.return_value.labware_clear_submission.return_value = {
                "cleared": ["lw-1"],
                "preserved_reuse_bound": [],
            }
            result = runner.invoke(
                app, ["labware", "clear-submission", "sub-xyz"],
            )
        assert result.exit_code == 0
        mock_cloud.return_value.labware_clear_submission.assert_called_once_with(
            "sub-xyz", force=False,
        )

    def test_discharge_no_prompt(self) -> None:
        with patch("orca.cli.labware.get_client") as mock_cloud:
            mock_cloud.return_value.labware_discharge.return_value = {
                "cleared_labware_ids": ["lw-1"],
            }
            result = runner.invoke(app, ["labware", "discharge", "lw-1"])
        assert result.exit_code == 0
        mock_cloud.return_value.labware_discharge.assert_called_once_with(
            "lw-1", force=False,
        )
