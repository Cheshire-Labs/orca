"""An operator's command counts as done only once its save has gone through."""

from tests.gateway._control_scope import StatesNothing
from collections.abc import Mapping
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import JsonValue

from cheshire_drivers.gateway_protocol import CommandMessage, MessageEnvelope
from orca.gateway.operator_commands import OperatorControl
from orca.gateway.controller.controller import DeviceController
from orca.gateway.controller.exceptions import CommandNotSavedError
from orca.modes.run_modes import WorkflowRunMode
from tests.gateway._control_scope import empty_runtime
from tests.gateway_parts import new_controller
from tests.gateway.test_an_operator_sim_command_waits_at_the_operator_speedup import _shaker


class _SaveFails(StatesNothing):
    async def save_completed(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, JsonValue],
        result: JsonValue,
        interfaces: frozenset[str],
    ) -> None:
        raise RuntimeError("the record is unavailable")


class _SaveFailsFirst(StatesNothing):
    def __init__(self, times: int) -> None:
        self.times = times
        self.tries = 0
        self.saved = 0

    async def save_completed(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, JsonValue],
        result: JsonValue,
        interfaces: frozenset[str],
    ) -> None:
        self.tries += 1
        if self.tries <= self.times:
            raise RuntimeError("the record is unavailable")
        self.saved += 1


def _answering_controller() -> tuple[DeviceController, MagicMock]:
    tracker, connections = MagicMock(), MagicMock()
    tracker.get_device = AsyncMock(return_value=_shaker())
    tracker.is_device_online = AsyncMock(return_value=True)
    tracker.get_client_for_device = AsyncMock(return_value="client_1")
    controller = new_controller(tracker, connections)

    async def answer(_client_id: str, raw: str) -> bool:
        command = CommandMessage.model_validate(MessageEnvelope.model_validate_json(raw).payload)
        controller._command_futures[command.command_id].set_result(None)
        return True

    connections.send_to_client = AsyncMock(side_effect=answer)
    return controller, connections


@pytest.mark.asyncio
async def test_a_command_whose_save_fails_fails_and_says_it_ran() -> None:
    controller, connections = _answering_controller()

    async with empty_runtime() as runtime:
        control = OperatorControl(runtime, "shaker_1", _shaker(), None, WorkflowRunMode.LIVE, _SaveFails(), controller)
        with pytest.raises(CommandNotSavedError, match="ran on 'shaker_1'.*the record is unavailable.*do not send it again"):
            await control.run("shake", {"speed": 500.0, "duration": 1.0}, timeout_seconds=5)

    assert connections.send_to_client.await_count == 1


@pytest.mark.asyncio
async def test_a_save_that_fails_twice_is_saved_on_its_third_try() -> None:
    controller, connections = _answering_controller()
    recorder = _SaveFailsFirst(2)

    async with empty_runtime() as runtime:
        control = OperatorControl(runtime, "shaker_1", _shaker(), None, WorkflowRunMode.LIVE, recorder, controller)
        await control.run("shake", {"speed": 500.0, "duration": 1.0}, timeout_seconds=5)

    assert (recorder.tries, recorder.saved) == (3, 1)
    assert connections.send_to_client.await_count == 1


@pytest.mark.asyncio
async def test_a_save_that_fails_four_times_fails_after_one_send() -> None:
    controller, connections = _answering_controller()
    recorder = _SaveFailsFirst(4)

    async with empty_runtime() as runtime:
        control = OperatorControl(runtime, "shaker_1", _shaker(), None, WorkflowRunMode.LIVE, recorder, controller)
        with pytest.raises(CommandNotSavedError, match="do not send it again"):
            await control.run("shake", {"speed": 500.0, "duration": 1.0}, timeout_seconds=5)

    assert (recorder.tries, recorder.saved) == (4, 0)
    assert connections.send_to_client.await_count == 1
