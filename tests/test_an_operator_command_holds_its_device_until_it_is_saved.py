"""An operator's device command keeps the device from its checks until its record is saved.

A second operator command on that device in the meantime is refused, never queued. The device stays under
external control until the last operator scope on it closes, so a run cannot take it while any is open.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import JsonValue

from orca.gateway import operator_recorder
from orca.gateway.controller.command_pipeline import NoAnswer, PartialChannelFailureError
from orca.gateway.controller.exceptions import CommandTimeoutError, DeviceLockedError
from orca.resource_models.command_effects import CommandStatement
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.state.records import ObservationGapCause
from orca.system.system_interface import ISystem
from tests.gateway._operator_command import run_operator_command
from tests.gateway_parts import gateway_with
from tests.test_adhoc_commands_are_recorded import WORKING_SITE, _Bench, _wire_to, running_lh_bench
from tests.test_labware_state_reconciliation import RESERVOIR_SITE

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]

Wire = Callable[..., Awaitable[JsonValue]]


def _move_to_the_working_site(bench: _Bench) -> dict[str, JsonValue]:
    return {
        "plate": bench.labware_name,
        "to_position": WORKING_SITE.split("/", 1)[1],
        "from_position": RESERVOIR_SITE.split("/", 1)[1],
    }


async def _as_operator(bench: _Bench, wire: Wire, command: str, params: dict[str, JsonValue]) -> None:
    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=MagicMock(interfaces=["ILiquidHandler"]))
    controller = MagicMock()
    controller.execute_command = AsyncMock(side_effect=wire)
    token = current_run_mode.set(WorkflowRunMode.LIVE)
    try:
        await run_operator_command(gateway_with(tracker=tracker, controller=controller), bench.runtime, bench.lh_name, command, params)
    finally:
        current_run_mode.reset(token)


async def test_a_second_operator_command_is_refused_until_the_first_is_saved_and_the_first_move_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saving = asyncio.Event()
    finish_saving = asyncio.Event()
    record = operator_recorder.record_operator_command

    async def held_record(
        system: ISystem, device_name: str, command: str, params: Mapping[str, JsonValue],
        result: JsonValue = None, *, interfaces: frozenset[str],
    ) -> None:
        if not saving.is_set():
            saving.set()
            await finish_saving.wait()
        await record(system, device_name, command, params, result, interfaces=interfaces)

    monkeypatch.setattr(operator_recorder, "record_operator_command", held_record)
    async with running_lh_bench() as bench:
        first = asyncio.create_task(_as_operator(bench, _wire_to(bench), "move_plate", _move_to_the_working_site(bench)))
        await asyncio.wait_for(saving.wait(), timeout=10)

        with pytest.raises(DeviceLockedError):
            await _as_operator(bench, _wire_to(bench), "move_plate", _move_to_the_working_site(bench))

        finish_saving.set()
        await asyncio.wait_for(first, timeout=10)
        assert bench.ledger_site() == WORKING_SITE


async def test_a_refused_operator_command_leaves_the_device_under_external_control_until_the_first_ends() -> None:
    async with running_lh_bench() as bench:
        device = bench.runtime.system.get_device(bench.lh_name)
        sending = asyncio.Event()
        finish_sending = asyncio.Event()
        wire = _wire_to(bench)

        async def held_wire(
            *, device_id: str, command: str, params: dict[str, JsonValue],
            **_: float | bool | WorkflowRunMode | frozenset[str] | None,
        ) -> JsonValue:
            sending.set()
            await finish_sending.wait()
            return await wire(device_id=device_id, command=command, params=params)

        first = asyncio.create_task(_as_operator(bench, held_wire, "move_plate", _move_to_the_working_site(bench)))
        await asyncio.wait_for(sending.wait(), timeout=10)
        with pytest.raises(DeviceLockedError):
            await _as_operator(bench, wire, "move_plate", _move_to_the_working_site(bench))

        assert device.under_external_control

        finish_sending.set()
        await asyncio.wait_for(first, timeout=10)
        assert not device.under_external_control


async def test_a_second_operator_command_is_refused_while_the_first_records_that_it_got_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_lh_bench() as bench:
        plate = bench.runtime.system.get_labware(bench.labware_name)
        flagging = asyncio.Event()
        finish_flagging = asyncio.Event()
        note_observation_gap = plate.note_observation_gap

        async def held_note_observation_gap(cause: ObservationGapCause) -> None:
            flagging.set()
            await finish_flagging.wait()
            await note_observation_gap(cause)

        monkeypatch.setattr(plate, "note_observation_gap", held_note_observation_gap)

        async def no_answer(**_: float | bool | str | WorkflowRunMode | frozenset[str] | dict[str, JsonValue] | None) -> JsonValue:
            raise CommandTimeoutError("no answer")

        first = asyncio.create_task(_as_operator(bench, no_answer, "move_plate", _move_to_the_working_site(bench)))
        await asyncio.wait_for(flagging.wait(), timeout=10)

        with pytest.raises(DeviceLockedError):
            await _as_operator(bench, _wire_to(bench), "move_plate", _move_to_the_working_site(bench))

        finish_flagging.set()
        with pytest.raises(CommandTimeoutError):
            await asyncio.wait_for(first, timeout=10)


async def test_a_second_operator_command_is_refused_while_the_first_reports_a_failed_channel_nothing_describes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_row_says_which_channel(
        system: ISystem, device_name: str, command: str, params: Mapping[str, JsonValue],
        result: JsonValue = None, *, interfaces: frozenset[str],
    ) -> None:
        raise PartialChannelFailureError([])

    monkeypatch.setattr(operator_recorder, "record_operator_command", no_row_says_which_channel)
    async with running_lh_bench() as bench:
        reporting = asyncio.Event()
        finish_reporting = asyncio.Event()
        no_answer = bench.runtime.no_answer

        async def held_no_answer(
            device_name: str, command: str, command_id: str, execution_id: str | None,
            seconds: float, why: NoAnswer, statement: CommandStatement | None, named: Sequence[str],
        ) -> str | None:
            if not reporting.is_set():
                reporting.set()
                await finish_reporting.wait()
            return await no_answer(device_name, command, command_id, execution_id, seconds, why, statement, named)

        monkeypatch.setattr(bench.runtime, "no_answer", held_no_answer)
        first = asyncio.create_task(_as_operator(bench, _wire_to(bench), "move_plate", _move_to_the_working_site(bench)))
        await asyncio.wait_for(reporting.wait(), timeout=10)

        with pytest.raises(DeviceLockedError):
            await _as_operator(bench, _wire_to(bench), "move_plate", _move_to_the_working_site(bench))

        finish_reporting.set()
        with pytest.raises(PartialChannelFailureError):
            await asyncio.wait_for(first, timeout=10)
