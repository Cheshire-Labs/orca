"""The one way a device command runs, whoever sent it: check, send, record.

A run, an operator and the engine all send through `DeviceCommandPipeline.run`. The record comes first, then each
registered effect in order, all before the command counts as done. Watchers hear of it after, from the block's
COMMAND events.
"""

import asyncio
import logging
import time
from contextlib import AbstractAsyncContextManager, AsyncExitStack, nullcontext
from enum import Enum
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Generic, Protocol, TypeVar

from pydantic import JsonValue

from cheshire_drivers.command_table import COMMANDS
from orca.gateway.controller.command_events import ErrorText, OneCommand, current_command, one_command, plain_error
from orca.gateway.controller.exceptions import (
    CommandNotSavedError, CommandOutcomeUnknownError, CommandTimeoutError, DeviceError,
)
from orca.resource_models.device_error import CommandTimeoutAbortedError
from orca.events.emitter import IEventEmitter
from orca.modes.run_modes import WorkflowRunMode, current_execution_id
from orca.state.command_operations import ChannelFailureNotReadError
from orca.state.records import OperationRecord

logger = logging.getLogger(__name__)

T = TypeVar("T")
S = TypeVar("S")
S_contra = TypeVar("S_contra", contravariant=True)

Check = Callable[[], Awaitable[None]]


class PartialChannelFailureError(RuntimeError):
    """A liquid handler call that returned per-channel failure records.

    Drivers signal partial failure by returning a structured response rather than raising. The record saves one row
    per affected well and raises this, so the command fails after its rows are saved.
    """

    def __init__(self, records: list[OperationRecord]) -> None:
        super().__init__(
            f"liquid handler call had {len(records)} per-channel records "
            f"(at least one channel errored)." if records else
            "liquid handler call had a failed channel and nothing says which channels worked, so no row was saved."
        )
        self.records = records


class ICommandEffect(Protocol[S_contra]):
    """One record a command leaves, written at each stage of the command from what the command states."""

    @property
    def runs_after_a_failed_save(self) -> bool:
        """True when this record follows the hardware even after an earlier record failed to save."""
        ...

    async def completed(self, statement: S_contra) -> None: ...

    async def failed(self, statement: S_contra) -> None: ...

    async def no_answer(self, statement: S_contra) -> None: ...


class NoAnswer(Enum):
    """Why nobody knows what a command did at the device."""

    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    TIMEOUT_ABORTED = "timeout_aborted"
    FAILED = "failed"
    """The device took the command and then reported that it failed. What it did before failing is unknown."""


class INoAnswerReporter(Protocol[S_contra]):
    """Told when what a command did is unknown: it got no answer, or the device failed it after taking it."""

    async def no_answer(
        self, device_name: str, command: str, command_id: str, execution_id: str | None,
        seconds: float, why: NoAnswer, statement: S_contra | None, named: Sequence[str],
    ) -> str | None:
        """Flag the labware as unobserved and file an incident to settle it. Returns its id.

        `named` is the labware the command names as its own. Empty means it names none, so it is about
        everything standing at the device.
        """
        ...


class INamedLabware(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def id(self) -> str: ...


class IDeviceWorlds(Protocol):
    """The driver worlds a command acts on, kept matching the record."""

    async def bring_up_to_date(self, device_name: str) -> None:
        """Before a command: rebuild the device's world from the record if it may no longer match."""
        ...

    async def mark_current_world(self, device_name: str) -> None:
        """After a sent command failed, got no answer or was not saved: its world may hold anything."""
        ...

    async def sent_to(self, device_name: str) -> None:
        """A command reached the device's world in force: its other worlds no longer hold what it does."""
        ...

    async def after_a_lifecycle_command(self, device_name: str) -> None:
        """After a lifecycle command answered: a device it brought up has its world rebuilt if it is marked."""
        ...


class IDeviceCommandRunner(Protocol):
    """What a device's commands run through: the system's one pipeline, seen from a device."""

    def labware_at(self, device_name: str) -> Sequence[INamedLabware]:
        """The labware standing at a device and its sites: what a command naming none of its own is about."""
        ...

    async def run_engine_command(
        self, device_name: str, command: str, call: Callable[[], Awaitable[T]],
        save: Callable[[], Awaitable[None]], *, carries_out: tuple[str, str] | None, world_upkeep: bool = False,
    ) -> T: ...

    async def run_device_command(
        self, device_name: str, command: str, send: Callable[[], Awaitable[T]],
        record: Callable[[T], Awaitable[None]], *, checks: Sequence[Check], execution_id: str | None,
        named: Sequence[str], params: Mapping[str, JsonValue] | None,
    ) -> T: ...


class IWireSender(Protocol):
    """The device controller, as the pipeline sends to it."""

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: dict[str, JsonValue] | None = None,
        timeout_seconds: float | None = None,
        *,
        effective_mode: WorkflowRunMode,
        effective_interfaces: frozenset[str] | None = None,
        confirm: bool = False,
    ) -> JsonValue: ...


async def send_to_the_device(
    controller: IWireSender,
    *,
    device_id: str,
    command: str,
    params: dict[str, JsonValue],
    timeout_seconds: float | None,
    effective_mode: WorkflowRunMode,
    effective_interfaces: frozenset[str] | None,
    confirm: bool,
) -> JsonValue:
    """The send stage of a command that names a device and no driver: an operator's command by name."""
    return await controller.execute_command(
        device_id=device_id,
        command=command,
        params=params,
        timeout_seconds=timeout_seconds,
        effective_mode=effective_mode,
        effective_interfaces=effective_interfaces,
        confirm=confirm,
    )


class DeviceCommandPipeline(Generic[S]):
    """Check, send and record one device command, with every effect in a fixed order."""

    def __init__(self, effects: Sequence[ICommandEffect[S]]) -> None:
        self._effects = tuple(effects)
        self._events: IEventEmitter | None = None
        self._error_text: ErrorText = plain_error
        self._no_answer: INoAnswerReporter[S] | None = None
        self._stop_reports: dict[asyncio.Future[str | None], str] = {}
        self._failed_stop_reports: list[str] = []
        self._worlds: IDeviceWorlds | None = None

    def labware_at(self, device_name: str) -> Sequence[INamedLabware]:
        return ()

    def keep_worlds_with(self, worlds: IDeviceWorlds) -> None:
        self._worlds = worlds

    async def apply_completed(self, statement: S) -> None:
        """A person says a command with no answer completed: its completed effects, as if it had answered."""
        async with self._held_while_recorded(statement):
            for effect in self._effects:
                await effect.completed(statement)

    def _held_while_recorded(self, statement: S) -> AbstractAsyncContextManager[None]:
        """What a completed command's effects run inside, so nobody acts on a device between them."""
        return nullcontext()

    def report_to(self, events: IEventEmitter, no_answer: INoAnswerReporter[S], error_text: ErrorText) -> None:
        """Where every command's events go, how their errors read, and who hears of a command with no answer.

        Set once the server starts, so a refusal reads the same whoever sent the command.
        """
        self._events = events
        self._no_answer = no_answer
        self._error_text = error_text

    async def run(
        self,
        device_name: str,
        command: str,
        send: Callable[[], Awaitable[T]],
        record: Callable[[T], Awaitable[None]],
        *,
        statement: S | None,
        checks: Sequence[Check],
        params: Mapping[str, JsonValue] | None,
        execution_id: str | None,
        events: IEventEmitter | None,
        carries_out: tuple[str, str] | None,
        named: Sequence[str] = (),
        hold: AbstractAsyncContextManager[None] | None = None,
        world_upkeep: bool = False,
    ) -> T:
        """Run one command. A block already open for it (the sender's own) is used.

        A different command sent while another runs (a door opened for an arm's pick) is a command of its own,
        with its own events. `named` is the labware the command names as its own, beyond what `statement` moves.
        `hold` is entered first, then the world stage, then the checks. The world stage rebuilds the device's world
        if it may not match the record. It does nothing for a command that is itself world upkeep (`world_upkeep`:
        it lays out, rebuilds or reads the driver's world). A lifecycle command runs before a world can be rebuilt,
        so its world is rebuilt after it answers, once the device is brought up. A sent command marks the device's
        other worlds; one that failed, got no answer or was not saved also marks its own, before the hold is left.
        `hold` is left right after the record is saved, before the completed
        effects, or after the failure records when the send fails or a channel fails. A stop leaves it at once: its
        records are written after, in a task of their own.
        """
        block = current_command()
        if block is not None and block.running and _stands_for(block, device_name, command, carries_out):
            # The step the running command is made of: its record is the running command's.
            async with hold if hold is not None else nullcontext():
                return await send()
        if block is not None and _stands_for(block, device_name, command, carries_out):
            if params is not None:
                # The entry that opened the block had the params as given; the sender's call has them as sent.
                block.context = block.context.model_copy(update={"params": dict(params)})
            return await self._stages(
                block, device_name, command, send, record, statement, checks, named, hold, world_upkeep,
            )
        # Every block sends its events, so a refusal ends FAILED for every sender.
        async with one_command(
            device_name, command, params, error_text=self._error_text, execution_id=execution_id,
            events=events if events is not None else self._events,
        ) as opened:
            try:
                return await self._stages(
                    opened, device_name, command, send, record, statement, checks, named, hold, world_upkeep,
                )
            finally:
                if block is not None and opened.reported_no_answer:
                    block.inner_reported_no_answer = True

    async def run_engine_command(
        self, device_name: str, command: str, call: Callable[[], Awaitable[T]],
        save: Callable[[], Awaitable[None]], *, carries_out: tuple[str, str] | None, world_upkeep: bool = False,
    ) -> T:
        """A command the engine sends a driver itself, saved by name."""
        async def record(_result: T) -> None:
            await save()

        return await self.run(
            device_name, command, call, record, statement=None, checks=(), params=None,
            execution_id=current_execution_id.get(), events=None, carries_out=carries_out, world_upkeep=world_upkeep,
        )

    async def run_device_command(
        self, device_name: str, command: str, send: Callable[[], Awaitable[T]],
        record: Callable[[T], Awaitable[None]], *, checks: Sequence[Check], execution_id: str | None,
        named: Sequence[str], params: Mapping[str, JsonValue] | None,
    ) -> T:
        """A command a run's action body sends a device: it names no labware motion of its own.

        `params` None takes the first wire command's, so a command refused before the wire names none.
        """
        return await self.run(
            device_name, command, send, record, statement=None, checks=checks, params=params,
            execution_id=execution_id, events=None, carries_out=None, named=named,
        )

    async def _stages(
        self, block: OneCommand, device_name: str, command: str, send: Callable[[], Awaitable[T]],
        record: Callable[[T], Awaitable[None]], statement: S | None, checks: Sequence[Check],
        named: Sequence[str], hold: AbstractAsyncContextManager[None] | None, world_upkeep: bool,
    ) -> T:
        was_running = block.running
        if block.names(device_name, command):
            block.running = True
        held = AsyncExitStack()
        try:
            if hold is not None:
                await held.enter_async_context(hold)
            await self._world_stage(device_name, command, world_upkeep)
            for check in checks:
                await check()
            try:
                result = await self._send(device_name, send)
            except asyncio.CancelledError:
                # The controller reserves the device without waiting, so a stop here lands on a command it is running.
                self._report_a_stop(block, device_name, command, statement, named)
                raise
            except CommandTimeoutAbortedError:
                await self._no_answer_stage(block, device_name, command, statement, NoAnswer.TIMEOUT_ABORTED, named)
                raise
            except CommandTimeoutError as timed_out:
                incident_id = await self._no_answer_stage(
                    block, device_name, command, statement, NoAnswer.TIMED_OUT, named,
                )
                if incident_id is None:
                    raise
                raise CommandOutcomeUnknownError(str(timed_out), incident_id) from timed_out
            except Exception as failure:
                written = await self._each_after_failure(statement, _failed)
                if not written or (isinstance(failure, DeviceError) and failure.may_have_moved):
                    await self._report_no_answer(block, device_name, command, statement, NoAnswer.FAILED, named)
                raise
            reported = False

            async def outcome_unknown() -> None:
                nonlocal reported
                if not reported:
                    reported = True
                    await self._report_no_answer(block, device_name, command, statement, NoAnswer.FAILED, named)

            try:
                await self._completed(
                    device_name, command, statement, lambda: record(result), held.aclose, outcome_unknown,
                )
            except PartialChannelFailureError as partial:
                await self._doubt_the_world(device_name)
                if not partial.records:
                    # A channel failed and no row says which: what the device now holds is unknown.
                    await outcome_unknown()
                raise
            except asyncio.CancelledError:
                # Stopped before its records landed: nobody knows what of them is true.
                await self._doubt_the_world(device_name)
                self._report_a_stop(block, device_name, command, statement, named)
                raise
            if self._worlds is not None and command in COMMANDS.lifecycle_names():
                await self._worlds.after_a_lifecycle_command(device_name)
            return result
        finally:
            await held.aclose()
            block.running = was_running

    async def _send(self, device_name: str, send: Callable[[], Awaitable[T]]) -> T:
        """The send, with the device's worlds marked before anything releases its hold."""
        try:
            result = await send()
        except BaseException:
            await self._doubt_the_world(device_name)
            raise
        if self._worlds is not None:
            await self._worlds.sent_to(device_name)
        return result

    async def _doubt_the_world(self, device_name: str) -> None:
        if self._worlds is not None:
            await self._worlds.mark_current_world(device_name)

    async def _world_stage(self, device_name: str, command: str, world_upkeep: bool) -> None:
        if self._worlds is None or world_upkeep or command in COMMANDS.lifecycle_names():
            return
        await self._worlds.bring_up_to_date(device_name)

    def _report_a_stop(
        self, block: OneCommand, device_name: str, command: str, statement: S | None, named: Sequence[str],
    ) -> None:
        """A stop's records are written beside the stop, not inside it: the stop must not wait on the record store."""
        will_report = self._reports_no_answer(block, device_name, command)
        report = asyncio.ensure_future(
            self._no_answer_stage(block, device_name, command, statement, NoAnswer.STOPPED, named),
        )
        # Set before the report is written: the command this one was sent inside reads it as the stop reaches it.
        block.reported_no_answer = block.reported_no_answer or will_report
        self._stop_reports[report] = f"{command} on {device_name}"
        report.add_done_callback(self._forget_stop_report)

    def _forget_stop_report(self, report: asyncio.Future[str | None]) -> None:
        stopped = self._stop_reports.pop(report, None)
        if stopped is not None and not report.cancelled() and report.exception() is not None:
            self._failed_stop_reports.append(stopped)

    async def stop_reports_written(self, timeout: float | None = None) -> list[str]:
        """Until every stop's records are written: for a reader that must see them, such as a shutdown or a test.

        Returns the stopped commands whose records failed, or were not written within `timeout`. Late writes are
        cancelled.
        """
        reports = dict(self._stop_reports)
        late: set[asyncio.Future[str | None]] = set()
        if reports:
            _done, late = await asyncio.wait(reports, timeout=timeout)
            for report in late:
                report.cancel()
            # Each done report's callback files a failure; let the queued ones run.
            await asyncio.sleep(0)
        failed, self._failed_stop_reports = self._failed_stop_reports, []
        return [*failed, *(reports[report] for report in late)]

    async def _no_answer_stage(
        self, block: OneCommand, device_name: str, command: str, statement: S | None, why: NoAnswer,
        named: Sequence[str],
    ) -> str | None:
        """Positions stay where they were and are flagged, then the device's own report."""
        await self._each_after_failure(statement, _no_answer)
        return await self._report_no_answer(block, device_name, command, statement, why, named)

    async def _report_no_answer(
        self, block: OneCommand, device_name: str, command: str, statement: S | None, why: NoAnswer,
        named: Sequence[str],
    ) -> str | None:
        """The device's own report goes once, for the sender's command.

        A report that cannot be filed fails the command as not saved, so it is never offered as a retry: nobody
        knows what the device did, and nobody was asked.
        """
        reporter = self._no_answer
        if reporter is None or not self._reports_no_answer(block, device_name, command):
            return None
        block.reported_no_answer = True
        try:
            return await reporter.no_answer(
                device_name, command, block.context.command_id, block.context.execution_id,
                time.monotonic() - block.started, why, statement, named,
            )
        except Exception as not_filed:
            raise CommandNotSavedError.of(device_name, command, not_filed) from not_filed

    def _reports_no_answer(self, block: OneCommand, device_name: str, command: str) -> bool:
        """Reported once: by the sender's own command, or by a step that ran before it started, and never again by
        a command that one sent inside it already reported."""
        if block.inner_reported_no_answer:
            return False
        return block.names(device_name, command) or not block.running

    async def _completed(
        self, device_name: str, command: str, statement: S | None, save: Callable[[], Awaitable[None]],
        release: Callable[[], Awaitable[None]], outcome_unknown: Callable[[], Awaitable[None]],
    ) -> None:
        not_saved: Exception | None = None
        try:
            await _saved_within_its_tries(save)
        except PartialChannelFailureError:
            if not await self._each_after_failure(statement, _failed):
                await outcome_unknown()
            raise
        except ChannelFailureNotReadError as not_read:
            if not await self._each_after_failure(statement, _failed):
                await outcome_unknown()
            raise PartialChannelFailureError([]) from not_read
        except Exception as exc:
            not_saved = exc
        # A failed channel raises above with the hold still taken: its no-answer flags are written under it.
        if not_saved is not None:
            await self._doubt_the_world(device_name)
        await release()
        if statement is not None:
            async with self._held_while_recorded(statement):
                for effect in self._effects:
                    if not_saved is not None and not effect.runs_after_a_failed_save:
                        continue
                    try:
                        await effect.completed(statement)
                    except Exception as exc:
                        if not_saved is None:
                            not_saved = exc
        if not_saved is not None:
            raise CommandNotSavedError.of(device_name, command, not_saved) from not_saved

    async def _each_after_failure(
        self, statement: S | None, stage: Callable[[ICommandEffect[S], S], Awaitable[None]],
    ) -> bool:
        """The failed or no-answer records. False when one could not be written.

        The command's own error is the one the sender needs, so the caller asks a person instead of raising.
        """
        if statement is None:
            return True
        written = True
        for effect in self._effects:
            try:
                await stage(effect, statement)
            except Exception:
                logger.exception("a command's records after its failure could not be written")
                written = False
        return written


SAVE_RETRIES = 3


async def _saved_within_its_tries(save: Callable[[], Awaitable[None]]) -> None:
    """A failed save is tried again, up to `SAVE_RETRIES` times. Each save is one transaction, so a failed try
    leaves no row. A failed channel is the device's answer, not a failed save, so it is raised at once."""
    for retries_left in range(SAVE_RETRIES, -1, -1):
        try:
            await save()
            return
        except (PartialChannelFailureError, ChannelFailureNotReadError):
            raise
        except Exception:
            if retries_left == 0:
                raise
            logger.warning("a command's record failed to save; trying again (%d left)", retries_left, exc_info=True)


async def _failed(effect: ICommandEffect[S], statement: S) -> None:
    await effect.failed(statement)


async def _no_answer(effect: ICommandEffect[S], statement: S) -> None:
    await effect.no_answer(statement)


def _stands_for(block: OneCommand, device_name: str, command: str, carries_out: tuple[str, str] | None) -> bool:
    return block.names(device_name, command) or (carries_out is not None and block.names(*carries_out))
