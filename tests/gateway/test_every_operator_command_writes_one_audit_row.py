"""Every operator command writes one audit row in the runner, whichever route sent it."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.command_table import COMMANDS
from cheshire_drivers.shaker_models import ShakeRequest

from orca.gateway.operator_commands import OperatorDeviceCommands

from tests.audit_capture import AuditCapture


def _runner(monkeypatch: pytest.MonkeyPatch) -> OperatorDeviceCommands:
    monkeypatch.setattr(OperatorDeviceCommands, "_send", AsyncMock(return_value=(None, 0.0)))
    return OperatorDeviceCommands(MagicMock(), lambda: None)


@pytest.mark.asyncio
async def test_a_typed_command_and_the_same_command_by_name_write_the_same_row(
    audit: AuditCapture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(monkeypatch)
    shake = COMMANDS.commands_of("IShaker")["shake"]

    await runner.run("shaker_1", shake, ShakeRequest(speed=500, duration=10), reason="typed")
    await runner.run_named("shaker_1", "shake", {"speed": 500, "duration": 10}, reason="by name")

    typed, by_name = [r for r in audit.rows() if r.action_name == "device.execute"]
    assert typed.call_args == by_name.call_args
    assert typed.call_args["command"] == "shake"
    assert (typed.reason, by_name.reason) == ("typed", "by name")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["initialize", "connect", "disconnect"])
async def test_a_lifecycle_command_writes_its_own_verb_and_no_generic_row(
    audit: AuditCapture, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    await _runner(monkeypatch).run_named("shaker_1", command, {}, reason="bring up")

    rows = audit.rows()
    assert [(r.action_name, r.reason) for r in rows] == [(f"device.{command}", "bring up")]


@pytest.mark.asyncio
async def test_a_read_writes_no_audit_row(audit: AuditCapture, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(monkeypatch)
    read = COMMANDS.commands_of("ITempGettable")["get_temperature"]

    await runner.run("shaker_1", read, read.validate({}))
    await runner.run_named("shaker_1", "get_temperature", {})

    assert audit.rows() == []
