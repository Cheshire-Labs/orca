"""`OperatorDeviceCommands`: a typed, a named and a held command each take external control and are saved to the record."""

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from cheshire_drivers.command_table import COMMANDS
import pytest_asyncio
from cheshire_drivers.liquid_handler_models import DeckStateResponse

from orca.gateway.operator_commands import OperatorDeviceCommands
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.state.records import DeviceOperation, TrackingSource
from tests.gateway_parts import gateway_with
from tests.test_adhoc_commands_are_recorded import _all_ops, _Bench, _wire_to, running_lh_bench

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def watched() -> AsyncIterator[tuple[_Bench, list[bool]]]:
    """A bench, and whether the device was under external control at each dispatch."""
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        async with running_lh_bench() as bench:
            yield bench, []
    finally:
        current_run_mode.reset(token)


def _runner(bench: _Bench, seen: list[bool]) -> OperatorDeviceCommands:
    device = bench.runtime.system.get_device(bench.lh_name)
    wire = _wire_to(bench)

    async def execute_command(**kwargs: Any) -> Any:
        seen.append(device.under_external_control)
        return await wire(**kwargs)

    snapshot = MagicMock()
    snapshot.interfaces = ["ILiquidHandler"]
    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=snapshot)
    tracker_controller = MagicMock()
    tracker_controller.execute_command = AsyncMock(side_effect=execute_command)
    tracker_controller.fault = MagicMock(return_value=None)
    return OperatorDeviceCommands(
        gateway_with(tracker=tracker, controller=tracker_controller), lambda: bench.runtime,
    )


async def _commands_saved(bench: _Bench) -> list[str]:
    return [
        str(op.details.model_dump().get("command"))
        for op in await _all_ops(bench)
        if op.device_name == bench.lh_name and op.source is TrackingSource.OPERATOR
        and op.operation is DeviceOperation.COMMAND
    ]


_GET_DECK_STATE = COMMANDS.commands_of("ILiquidHandler")["get_deck_state"]


async def test_a_typed_command_takes_external_control_and_is_saved(watched: tuple[_Bench, list[bool]]) -> None:
    bench, seen = watched

    answer = await _runner(bench, seen).run(bench.lh_name, _GET_DECK_STATE, _GET_DECK_STATE.validate({}))

    assert isinstance(answer, DeckStateResponse)
    assert seen == [True]
    assert await _commands_saved(bench) == ["get_deck_state"]
    assert not bench.runtime.system.get_device(bench.lh_name).under_external_control


async def test_a_named_command_takes_external_control_and_is_saved(watched: tuple[_Bench, list[bool]]) -> None:
    bench, seen = watched

    ran = await _runner(bench, seen).run_named(bench.lh_name, "get_deck_state", {})

    assert isinstance(ran.value, dict)
    assert seen == [True]
    assert await _commands_saved(bench) == ["get_deck_state"]


async def test_a_held_device_keeps_external_control_between_its_commands(watched: tuple[_Bench, list[bool]]) -> None:
    bench, seen = watched
    device = bench.runtime.system.get_device(bench.lh_name)
    between: list[bool] = []

    async with _runner(bench, seen).hold(bench.lh_name) as held:
        await held.run(bench.lh_name, _GET_DECK_STATE, _GET_DECK_STATE.validate({}))
        between.append(device.under_external_control)
        await held.run_named(bench.lh_name, "get_deck_state", {})

    assert seen == [True, True]
    assert between == [True], "the engine could take the device between two held commands"
    assert await _commands_saved(bench) == ["get_deck_state", "get_deck_state"]
    assert not device.under_external_control
