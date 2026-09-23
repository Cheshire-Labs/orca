"""Id-prefix resolution on ``orca incident get`` / ``orca incident ack`` /
``orca submission detail``.

Where a verb reads from a single source, prefix support alone is
enough: the ``IncidentService`` backs both
``list`` and ``get`` from the same store, and ``SubmissionFacade``
walks the same ``iter_executions()`` for both verbs, so neither domain
needs a labware-style daemon pass-through. What was missing was prefix
support on these verbs at all.

The cloud-only ``incident recoverable-timeout {extend,abort,mark-complete}``
verbs still require full UUIDs (no cloud listing endpoint on the CLI;
same precedent as ``labware journey``); the verb-help string is the
operator-facing contract there.
"""

from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.daemon.schemas import IncidentDTO, SubmissionDTO


runner = CliRunner()


@pytest.fixture(autouse=True)
def _force_local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Incident prefix-resolution is backend-aware; pin it local so
    these local-path tests don't trip backend auto-resolution."""
    monkeypatch.setattr("orca.cli.incident.active_backend", lambda: "local")


def _incident(**overrides: Any) -> IncidentDTO:
    base: dict[str, Any] = {
        "id": "7faedeec-1234-4abc-9def-000000000001",
        "timestamp": 1700000000.0,
        "category": "VARIABLE_RESOLUTION",
        "severity": "ERROR",
        "execution_id": "exec-1",
        "thread_id": None,
        "message": "missing variable 'plate_count'",
        "detail": {"var_name": "plate_count", "action_command": None},
        "recovery_action": "THREAD_RECOVER_RETRY",
        "acknowledged": False,
    }
    base.update(overrides)
    return IncidentDTO(**base)


def _submission(**overrides: Any) -> SubmissionDTO:
    base: dict[str, Any] = {
        "id": "abcdeeee-1111-4abc-9def-000000000001",
        "execution_id": "exec-1",
        "workflow_name": "smc_assay",
        "group_count": 1,
        "status": "ACCEPTED",
        "batch_mode": "STANDALONE",
        "submitted_at": "2026-05-26T12:00:00+00:00",
        "run_mode": "PURE_SIM",
        "operator_id": None,
        "deployment_profile": None,
    }
    base.update(overrides)
    return SubmissionDTO(**base)


# -- incident get: prefix resolution ----------------------------------------


class TestIncidentGetPrefix:
    def test_full_id_resolves_and_calls_get(self) -> None:
        inc = _incident()
        with patch("orca.cli.incident.local_client") as mock_local:
            mock_local.return_value.incidents_list.return_value = [inc]
            mock_local.return_value.incidents_get.return_value = inc
            result = runner.invoke(app, ["incident", "get", inc.id])
        assert result.exit_code == 0, result.output
        mock_local.return_value.incidents_get.assert_called_once_with(inc.id)

    def test_unique_prefix_resolves(self) -> None:
        inc = _incident()
        with patch("orca.cli.incident.local_client") as mock_local:
            mock_local.return_value.incidents_list.return_value = [inc]
            mock_local.return_value.incidents_get.return_value = inc
            result = runner.invoke(app, ["incident", "get", "7faedeec"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.incidents_get.assert_called_once_with(inc.id)

    def test_ambiguous_prefix_fails_locally(self) -> None:
        a = _incident(id="abcdeeee-1111-4abc-9def-000000000001")
        b = _incident(id="abcdffff-2222-4abc-9def-000000000002")
        with patch("orca.cli.incident.local_client") as mock_local:
            mock_local.return_value.incidents_list.return_value = [a, b]
            result = runner.invoke(app, ["incident", "get", "abcd"])
        assert result.exit_code != 0
        assert "abcdeeee" in result.output
        assert "abcdffff" in result.output
        mock_local.return_value.incidents_get.assert_not_called()

    def test_unknown_id_fails_not_found(self) -> None:
        with patch("orca.cli.incident.local_client") as mock_local:
            mock_local.return_value.incidents_list.return_value = []
            result = runner.invoke(app, ["incident", "get", "deadbeef-9999"])
        assert result.exit_code != 0
        mock_local.return_value.incidents_get.assert_not_called()


# -- incident ack: prefix resolution + --force bypass -----------------------


class TestIncidentAckPrefix:
    def test_unique_prefix_resolves_and_acks(self) -> None:
        inc = _incident()
        with patch("orca.cli.incident.local_client") as mock_local:
            mock_local.return_value.incidents_list.return_value = [inc]
            result = runner.invoke(
                app, ["--force", "incident", "ack", "7faedeec"],
            )
        assert result.exit_code == 0, result.output
        mock_local.return_value.incidents_ack.assert_called_once_with(inc.id)

    def test_ambiguous_prefix_does_not_ack(self) -> None:
        a = _incident(id="abcdeeee-1111-4abc-9def-000000000001")
        b = _incident(id="abcdffff-2222-4abc-9def-000000000002")
        with patch("orca.cli.incident.local_client") as mock_local:
            mock_local.return_value.incidents_list.return_value = [a, b]
            result = runner.invoke(
                app, ["--force", "incident", "ack", "abcd"],
            )
        assert result.exit_code != 0
        assert "abcdeeee" in result.output
        mock_local.return_value.incidents_ack.assert_not_called()

    def test_full_id_acks_without_resolution_drift(self) -> None:
        """Full id matches exactly; resolver returns it untouched."""
        inc = _incident()
        with patch("orca.cli.incident.local_client") as mock_local:
            mock_local.return_value.incidents_list.return_value = [inc]
            result = runner.invoke(
                app, ["--force", "incident", "ack", inc.id],
            )
        assert result.exit_code == 0, result.output
        mock_local.return_value.incidents_ack.assert_called_once_with(inc.id)


# -- submission detail: prefix resolution -----------------------------------


class TestSubmissionDetailPrefix:
    def test_full_id_resolves_and_calls_get(self) -> None:
        snap = _submission()
        with patch("orca.cli.submission.get_client") as mock_local:
            mock_local.return_value.submissions_list.return_value = [snap]
            mock_local.return_value.submission_get.return_value = snap
            result = runner.invoke(app, ["submission", "detail", snap.id])
        assert result.exit_code == 0, result.output
        mock_local.return_value.submission_get.assert_called_once_with(snap.id)

    def test_unique_prefix_resolves(self) -> None:
        snap = _submission()
        with patch("orca.cli.submission.get_client") as mock_local:
            mock_local.return_value.submissions_list.return_value = [snap]
            mock_local.return_value.submission_get.return_value = snap
            result = runner.invoke(
                app, ["submission", "detail", "abcdeeee"],
            )
        assert result.exit_code == 0, result.output
        mock_local.return_value.submission_get.assert_called_once_with(snap.id)

    def test_ambiguous_prefix_fails_locally(self) -> None:
        a = _submission(id="abcdeeee-1111-4abc-9def-000000000001")
        b = _submission(id="abcdffff-2222-4abc-9def-000000000002")
        with patch("orca.cli.submission.get_client") as mock_local:
            mock_local.return_value.submissions_list.return_value = [a, b]
            result = runner.invoke(app, ["submission", "detail", "abcd"])
        assert result.exit_code != 0
        assert "abcdeeee" in result.output
        assert "abcdffff" in result.output
        mock_local.return_value.submission_get.assert_not_called()

    def test_unknown_id_fails_not_found(self) -> None:
        with patch("orca.cli.submission.get_client") as mock_local:
            mock_local.return_value.submissions_list.return_value = []
            result = runner.invoke(
                app, ["submission", "detail", "deadbeef-9999"],
            )
        assert result.exit_code != 0
        mock_local.return_value.submission_get.assert_not_called()
