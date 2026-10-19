"""The CLI verbs this change audits carry `--reason` to the control-plane client."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from orca.cli.app import app

runner = CliRunner()

EID = "11111111-2222-3333-4444-555555555555"

CASES: list[tuple[str, list[str], str]] = [
    ("orca.cli.backend.get_client", ["run", "wf", "--run-mode", "PURE_SIM"], "submit_workflow"),
    ("orca.cli.execution.get_client", ["execution", "stop", EID], "stop_execution"),
    ("orca.cli.execution.get_client", ["execution", "remove", EID], "execution_remove"),
    ("orca.cli.manual_step.local_client", ["manual-step", "confirm", "s1", "--execution", EID],
     "manual_step_confirm"),
    ("orca.cli.incident.get_client",
     ["incident", "recoverable-timeout", "extend", "inc-1", "--additional-seconds", "30"],
     "recoverable_timeout_extend"),
    ("orca.cli.labware.get_client", ["labware", "clear-submission", "sub-1"],
     "labware_clear_submission"),
    ("orca.cli.labware.get_client", ["labware", "clear-all", "--yes"], "labware_clear_all"),
    ("orca.cli.device.get_client", ["device", "connect", "shaker1"], "device_connect"),
]


@pytest.mark.parametrize("target,argv,method", CASES, ids=[c[2] for c in CASES])
def test_the_reason_reaches_the_client(target: str, argv: list[str], method: str) -> None:
    client = MagicMock()
    client.list_executions.return_value = [SimpleNamespace(id=EID)]
    with patch(target, return_value=client), \
            patch("orca.cli.manual_step.active_backend", return_value="local"), \
            patch("orca.cli.resolve.record_last_execution"):
        result = runner.invoke(app, ["--force", *argv, "--reason", "why"])

    call = getattr(client, method)
    assert call.called, f"{' '.join(argv)} never reached {method}: {result.output}"
    assert call.call_args.kwargs["reason"] == "why"
