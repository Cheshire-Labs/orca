"""A command whose record cannot be written is never reported as done, and never left with only a log line.

A completed command whose position cannot be written fails as not saved. A failed command whose records cannot be
written asks a person, as one with no answer does. A report that cannot be filed fails the command.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from unittest.mock import AsyncMock, Mock

import pytest

from orca.gateway.controller.command_pipeline import (
    DeviceCommandPipeline, ICommandEffect, NoAnswer, PartialChannelFailureError,
)
from orca.gateway.controller.exceptions import CommandExecutionError, CommandNotSavedError
from orca.resource_models.command_effects import (
    Carried, CommandStatement, DeckRecordEffect, LabwareMotion, LabwarePositionEffect,
)
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placement import LabwarePlacer
from orca.resource_models.location import Location
from orca.state.command_operations import ChannelFailureNotReadError

pytestmark = pytest.mark.timeout(30)

Send = Callable[[], Awaitable[None]]


class _Reporter:
    def __init__(self, fails: bool = False) -> None:
        self.reports: list[NoAnswer] = []
        self._fails = fails

    async def no_answer(
        self, device_name: str, command: str, command_id: str, execution_id: str | None,
        seconds: float, why: NoAnswer, statement: CommandStatement | None, named: Sequence[str],
    ) -> str | None:
        if self._fails:
            raise RuntimeError("the incident store is down")
        self.reports.append(why)
        return "incident-1"


class _FailedRecordBreaks:
    """An effect whose failed and no-answer records cannot be written."""

    @property
    def runs_after_a_failed_save(self) -> bool:
        return True

    async def completed(self, statement: CommandStatement) -> None:
        return None

    async def failed(self, statement: CommandStatement) -> None:
        raise RuntimeError("the record store is down")

    async def no_answer(self, statement: CommandStatement) -> None:
        raise RuntimeError("the record store is down")


def _a_move() -> CommandStatement:
    motion = LabwareMotion(
        Mock(spec=LabwareInstance), Mock(spec=Location), Mock(spec=Location), Carried.MOVED_BY_DEVICE,
        failed_back_to=None,
    )
    return CommandStatement(device_name="lh", motion=motion, site=None)


def _pipeline(
    *effects: ICommandEffect[CommandStatement], reporter: _Reporter | None = None,
) -> DeviceCommandPipeline[CommandStatement]:
    pipeline = DeviceCommandPipeline[CommandStatement](effects)
    pipeline.report_to(Mock(), reporter or _Reporter(), str)
    return pipeline


async def _send_ok() -> None:
    return None


async def _record(_result: None) -> None:
    return None


async def _refused() -> None:
    raise CommandExecutionError("the device refused it")


async def _run(pipeline: DeviceCommandPipeline[CommandStatement], send: Send = _send_ok) -> None:
    await pipeline.run(
        "lh", "move_plate", send, _record, statement=_a_move(), checks=(), params=None,
        execution_id=None, events=None, carries_out=None,
    )


async def test_a_move_onto_a_position_the_record_has_occupied_fails_as_not_saved() -> None:
    placer = Mock(spec=LabwarePlacer)
    placer.relocate = AsyncMock(side_effect=SlotOccupiedError("lh/site-1", "plate_b", "plate_96"))
    conflicts: list[str] = []
    deck = Mock(spec=DeckRecordEffect)
    deck.runs_after_a_failed_save = False
    position = LabwarePositionEffect(placer, lambda device, _motion, _occupied: conflicts.append(device))
    pipeline = _pipeline(position, deck)

    with pytest.raises(CommandNotSavedError, match="do not send it again"):
        await _run(pipeline)

    assert conflicts == ["lh"]
    deck.completed.assert_not_called()


async def test_a_failed_command_whose_records_cannot_be_written_asks_a_person() -> None:
    reporter = _Reporter()
    pipeline = _pipeline(_FailedRecordBreaks(), reporter=reporter)

    with pytest.raises(CommandExecutionError, match="refused"):
        await _run(pipeline, _refused)

    assert reporter.reports == [NoAnswer.FAILED]


async def test_a_no_answer_report_that_cannot_be_filed_fails_the_command_as_not_saved() -> None:
    """Not saved, so a run never offers it as a retry: nobody knows what the device did, and nobody was asked."""
    pipeline = _pipeline(_FailedRecordBreaks(), reporter=_Reporter(fails=True))

    with pytest.raises(CommandNotSavedError, match="incident store is down"):
        await _run(pipeline, _refused)


async def test_a_stop_whose_records_cannot_be_written_is_named_at_shutdown() -> None:
    pipeline = _pipeline(_FailedRecordBreaks(), reporter=_Reporter(fails=True))
    started = asyncio.Event()

    async def runs_until_stopped() -> None:
        started.set()
        await asyncio.Event().wait()

    command = asyncio.create_task(_run(pipeline, runs_until_stopped))
    await started.wait()
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    # The report fails long before shutdown asks.
    for _ in range(10):
        await asyncio.sleep(0)

    assert await pipeline.stop_reports_written(timeout=5) == ["move_plate on lh"]
    assert await pipeline.stop_reports_written(timeout=5) == []


class _EngineCommand:
    """An engine command whose save fails its first `times` tries."""

    def __init__(self, times: int) -> None:
        self.times = times
        self.sent = 0
        self.tries = 0
        self.saved = 0

    async def call(self) -> str:
        self.sent += 1
        return "initialized"

    async def save(self) -> None:
        self.tries += 1
        if self.tries <= self.times:
            raise OSError("disk full")
        self.saved += 1


async def test_an_engine_command_whose_save_fails_twice_is_saved_on_its_third_try() -> None:
    command = _EngineCommand(2)

    assert await _pipeline().run_engine_command(
        "lh", "initialize", command.call, command.save, carries_out=None,
    ) == "initialized"

    assert (command.sent, command.tries, command.saved) == (1, 3, 1)


async def test_an_engine_command_whose_save_fails_four_times_fails_as_not_saved_after_one_send() -> None:
    command = _EngineCommand(4)

    with pytest.raises(CommandNotSavedError, match="disk full"):
        await _pipeline().run_engine_command("lh", "initialize", command.call, command.save, carries_out=None)

    assert (command.sent, command.tries, command.saved) == (1, 4, 0)


@pytest.mark.parametrize("failure", [PartialChannelFailureError([]), ChannelFailureNotReadError("discard_tips")])
async def test_a_failed_channel_is_saved_once_and_never_tried_again(failure: Exception) -> None:
    tries = 0

    async def save(_result: None) -> None:
        nonlocal tries
        tries += 1
        raise failure

    with pytest.raises(PartialChannelFailureError):
        await _pipeline().run(
            "lh", "discard_tips", _send_ok, save, statement=None, checks=(), params=None,
            execution_id=None, events=None, carries_out=None,
        )

    assert tries == 1
