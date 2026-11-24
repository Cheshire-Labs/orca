"""`OperatorDeviceCommands` hands back what the driver said: whole by name, typed for a declared command, refusals raised."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.command_table import COMMANDS
from cheshire_drivers.command_responses import TemperatureResponse
from pydantic import JsonValue

from orca.gateway.controller.exceptions import ConfirmationRequiredError, DeviceOfflineError
from orca.gateway.operator_commands import DriverAnswer, OperatorControl, OperatorDeviceCommands
from tests.gateway._control_scope import stub_control_scope
from tests.gateway_parts import gateway_with

pytestmark = pytest.mark.asyncio

# What the PF400 driver really answers with: it keys the moved axes by an integer enum, and JSON has only string keys.
_RECOVERED: dict[str, JsonValue] = {"2": 12.5, "4": -3.0}


@pytest.fixture
def answered(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stub_control_scope(monkeypatch)
    run = AsyncMock()
    monkeypatch.setattr(OperatorControl, "run", run)
    return run


def _runner() -> OperatorDeviceCommands:
    return OperatorDeviceCommands(gateway_with(controller=MagicMock()), lambda: None)


async def test_a_vendor_extras_answer_comes_back_whole(answered: AsyncMock) -> None:
    """A vendor extra has no response model; forcing it through one would throw the answer away."""
    answered.return_value = DriverAnswer(raw=_RECOVERED, interfaces=frozenset({"ITransporter"}))

    ran = await _runner().run_named("pf400_1", "recover_axes_within_limits", {})

    assert ran.value == _RECOVERED
    assert ran.duration_ms >= 0


async def test_the_confirm_gate_is_passed_through(answered: AsyncMock) -> None:
    answered.return_value = DriverAnswer(raw=None, interfaces=frozenset())

    await _runner().run_named("pf400_1", "send_command", {"line": "hp 1"}, confirm=True)

    assert answered.await_args_list[-1].kwargs["confirm"] is True


@pytest.mark.parametrize("refusal", [
    ConfirmationRequiredError("send_command on pf400_1 is a vendor command not declared read-safe"),
    DeviceOfflineError("pf400_1 is offline"),
])
async def test_a_refusal_reaches_the_caller(answered: AsyncMock, refusal: Exception) -> None:
    answered.side_effect = refusal

    with pytest.raises(type(refusal)):
        await _runner().run_named("pf400_1", "send_command", {"line": "hp 1"})


async def test_a_declared_command_comes_back_as_its_response_model(answered: AsyncMock) -> None:
    """`get_temperature`, not an ack-only command: a None answer is an empty response whatever the lookup says."""
    answered.return_value = DriverAnswer(raw={"temperature": 25.0}, interfaces=frozenset({"ITempGettable"}))

    get_temperature = COMMANDS.commands_of("ITempGettable")["get_temperature"]
    response = await _runner().run("shaker_1", get_temperature, get_temperature.validate({}))

    assert isinstance(response, TemperatureResponse)
    assert response.temperature == 25.0
