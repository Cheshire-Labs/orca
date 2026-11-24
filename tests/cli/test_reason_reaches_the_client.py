"""Every audited CLI verb carries `--reason` to the control-plane client.

The client puts it on the wire, and the server writes it onto the audit row.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from orca.cli.app import app

runner = CliRunner()

EID = "11111111-2222-3333-4444-555555555555"
TID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
IID = "99999999-8888-7777-6666-555555555555"
LID = "lw-0000000000"

CASES: list[tuple[str, list[str], str]] = [
    ("device", ["device", "initialize", "d"], "device_initialize"),
    ("device", ["device", "disconnect", "d"], "device_disconnect"),
    ("device", ["device", "reconcile-deck", "d"], "device_reconcile_deck"),
    ("device", ["device", "release-control", "d"], "device_release_control"),
    ("device", ["device", "clear-fault", "d"], "device_clear_fault"),
    ("device", ["device", "connect", "d"], "device_connect"),
    ("execution", ["execution", "stop", EID], "stop_execution"),
    ("execution", ["execution", "remove", EID], "execution_remove"),
    ("incident", ["incident", "recoverable-timeout", "extend", IID, "--additional-seconds", "30"],
     "recoverable_timeout_extend"),
    ("labware", ["labware", "clear-submission", "sub-1"], "labware_clear_submission"),
    ("labware", ["labware", "clear-all", "--yes"], "labware_clear_all"),
    ("execution", ["execution", "thread", "spawn", EID, "tmpl"], "spawn_thread"),
    ("labware", ["labware", "carry", LID, "--speed", "20"], "labware_set_carry_override"),
    ("labware", ["labware", "carry-normally", LID], "labware_clear_carry_override"),
    ("labware", ["labware", "edit-barcode", LID, "BC-1"], "labware_edit_barcode"),
    ("labware", ["labware", "register", "plate_96"], "labware_register"),
    ("var", ["var", "set", "x", "1", "--execution", EID], "variables_set"),
    ("var", ["var", "set-global", "x", "1"], "variables_set_global"),
    ("var", ["var", "unset", "x", "--execution", EID], "variables_unset"),
    ("access_config", ["access-configs", "create", "a", "--access-type", "vertical"], "access_configs_add"),
    ("access_config", ["access-configs", "update", "a", "--access-type", "vertical"], "access_configs_update"),
    ("access_config", ["access-configs", "delete", "a"], "access_configs_delete"),
    ("profile", ["profiles", "create", "a"], "profiles_add"),
    ("profile", ["profiles", "update", "a"], "profiles_update"),
    ("profile", ["profiles", "delete", "a"], "profiles_delete"),
    ("teachpoint", ["teachpoints", "delete", "r", "p"], "teachpoints_delete"),
    ("teachpoint", ["teachpoints", "labware", "clear", "r", "p", "t"], "teachpoints_clear_labware_override"),
    ("deck_layout", ["deck-layouts", "delete", "lh", "n"], "deck_layouts_delete"),
    ("grip_profiles", ["grip-profiles", "reset", "t"], "grip_profiles_reset"),
    ("move_defaults", ["move-defaults", "reset", "arm"], "move_defaults_reset"),
    ("execution", ["execution", "close", EID], "execution_close"),
    ("incident", ["incident", "ack", IID], "incidents_ack"),
    ("incident", ["incident", "ack-all"], "incidents_ack_all"),
    ("manual_step", ["manual-step", "confirm", "s1", "--execution", EID], "manual_step_confirm"),
]


def _client() -> MagicMock:
    client = MagicMock()
    client.list_executions.return_value = [SimpleNamespace(id=EID)]
    client.get_execution.return_value.threads = [SimpleNamespace(id=TID)]
    client.incidents_list.return_value = [SimpleNamespace(id=IID)]
    client.device_clear_fault.return_value.cleared = None
    return client


@pytest.mark.parametrize("module,argv,method", CASES, ids=[c[2] for c in CASES])
def test_the_reason_reaches_the_client(module: str, argv: list[str], method: str) -> None:
    client = _client()
    with patch(f"orca.cli.{module}.get_client", return_value=client):
        runner.invoke(app, ["--force", *argv, "--reason", "why"])

    call = getattr(client, method)
    assert call.called, f"{' '.join(argv)} never reached {method}"
    assert call.call_args.kwargs["reason"] == "why"


def test_the_reason_reaches_the_cloud_on_workflow_load(tmp_path: Path) -> None:
    source = tmp_path / "wf.py"
    source.write_text("# wf\n")
    client = MagicMock()
    with (
        patch("orca.cli.registry.cloud_client", return_value=client),
        patch("orca.cli.backend.active_backend", return_value="cloud"),
    ):
        runner.invoke(app, ["workflow", "load", str(source), "--name", "wf", "-m", "m", "--reason", "why"])

    assert client.workflow_load.call_args.kwargs["reason"] == "why"


def test_the_reason_reaches_the_command_sent_by_name() -> None:
    """`orca device send` puts the reason on the batch it sends."""
    client = _client()
    with patch("orca.cli.device.get_client", return_value=client):
        runner.invoke(app, ["--force", "device", "send", "d", "noop", "--reason", "why"])

    _, request = client.device_operator_command.call_args.args
    assert request.reason == "why"
