"""A verb asked for --json prints its answer as JSON, and a verb that succeeded in table mode prints something."""

import errno
import json

import pytest
from cheshire_drivers.device_setup import DeviceSetup
from pydantic import BaseModel, JsonValue
from typer.testing import CliRunner

from orca.cli import __main__ as entry
from orca.cli import device, device_operator, output, state
from orca.cli.app import app
from orca.cli.output import OutputMode
from orca.operations.device_models import CompareDeckResponse, GetMountedTipsResponse, ReconcileDeckResponse
from orca.operations.device_setup_models import DeviceSetupResponse
from orca.operations.operator_device_models import BATCH_EXECUTE, BatchExecuteResponse, OperatorRoute
from orca.operations.state_models import UnsettledStateResponse
from orca.operations.wire_models import TeachpointDTO


class _Client:
    def state_unsettled(self) -> UnsettledStateResponse:
        return UnsettledStateResponse(unsettled=[])

    def device_get_mounted_tips(self, device_name: str) -> GetMountedTipsResponse:
        return GetMountedTipsResponse(device_name=device_name, mounted=[], provenance="unknown")

    def device_compare_deck(self, device_name: str) -> CompareDeckResponse:
        return CompareDeckResponse(device_name=device_name, agrees=True, driver_deck_empty=False)

    def device_reconcile_deck(self, device_name: str, *, reason: str | None = None) -> ReconcileDeckResponse:
        return ReconcileDeckResponse(device_name=device_name)

    def device_setup_get(self, device_name: str) -> DeviceSetupResponse:
        return DeviceSetupResponse(device_name=device_name, setup=DeviceSetup(client="lab_pc"))

    def device_operator_command(self, route: OperatorRoute, req: BaseModel) -> BaseModel:
        if route is BATCH_EXECUTE:
            return BatchExecuteResponse(success=True, results=[], total_duration_ms=1.0)
        return TeachpointDTO(
            device_id="arm", position_id="hotel_1", coord_type="cartesian", coords={"x": 1.0},
            access_config_name=None, gateway=None, orientation="left",
        )


@pytest.fixture(autouse=True)
def client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Client()
    for module in (state, device, device_operator):
        monkeypatch.setattr(module, "get_client", lambda: fake)
    monkeypatch.setattr(output, "_mode", OutputMode.TABLE)


@pytest.mark.parametrize(("argv", "field", "value"), [
    (["state", "unsettled"], "unsettled", []),
    (["device", "mounted-tips", "lh"], "provenance", "unknown"),
    (["device", "compare-deck", "lh"], "agrees", True),
    (["device", "reconcile-deck", "lh"], "status", "reconciled"),
])
def test_a_read_asked_for_json_prints_its_answer_as_json(argv: list[str], field: str, value: JsonValue) -> None:
    result = CliRunner().invoke(app, ["--json", *argv])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[field] == value


@pytest.mark.parametrize(("argv", "shown"), [
    (["teachpoints", "capture", "arm", "hotel_1", "--orientation", "left"], "hotel_1"),
    (["device", "batch", "shaker", "--commands", '[{"command": "open"}]'], "total_duration_ms"),
    (["device", "setup", "shaker"], "lab_pc"),
])
def test_a_verb_that_succeeded_in_table_mode_prints_its_answer(argv: list[str], shown: str) -> None:
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert shown in result.stdout


@pytest.mark.parametrize("closed", [BrokenPipeError(errno.EPIPE, "closed"), OSError(errno.EINVAL, "Invalid argument")])
def test_a_reader_that_closed_the_pipe_ends_the_command_with_no_traceback(
    monkeypatch: pytest.MonkeyPatch, closed: OSError,
) -> None:
    def write_to_a_closed_pipe() -> None:
        raise closed

    discarded: list[bool] = []
    monkeypatch.setattr(entry, "app", write_to_a_closed_pipe)
    monkeypatch.setattr(entry, "_discard_what_is_left_to_write", lambda: discarded.append(True))

    with pytest.raises(SystemExit) as ended:
        entry.main()

    assert ended.value.code == 1
    assert discarded == [True]


def test_an_os_error_that_is_not_a_closed_pipe_is_still_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise FileNotFoundError(errno.ENOENT, "missing")

    monkeypatch.setattr(entry, "app", fail)

    with pytest.raises(FileNotFoundError):
        entry.main()
